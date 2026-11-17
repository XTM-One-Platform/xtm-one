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

"""Built-in presentation tools — agent-generated interactive decks.

Gated behind the ``generate_presentation`` built-in tool id:

* ``generate_presentation`` — create a deck from a structured
  ``PresentationSpec``, or rewrite an existing one WHOLESALE by passing its
  ``presentation_id`` (every slide re-submitted).
* ``edit_presentation`` — the surgical path: named operations applied
  server-side to the STORED spec — one slide, one block, one word — with every
  slide no operation names proven byte-identical afterwards
  (``services/presentation_patch.py``). This is the tool for ANY change to an
  existing deck: a model re-emitting a 39-slide deck to change one word never
  emits the same 39 slides, so the full-spec path drifts what the user did not
  ask about.
* ``get_presentation`` — read a deck's current spec back, so an agent can edit a
  deck it produced in an earlier turn without having the JSON in context. Never
  cut mid-JSON: the outline (with slide ids) is always complete, and a spec over
  the per-call budget is served whole slide by whole slide, with a ``slides``
  selector (``"12"``, ``"16-25"``, ids) to read any part of a long deck in full.
* ``duplicate_presentation`` — copy a deck the user can read into a new deck
  they own, under a new title: the library's ``Duplicate``, through the
  same service (``presentations.duplicate_presentation``). The copy keeps the
  template link, theme, aspect ratio, chrome and every slide with its id, and
  none of the original's sharing or comments; the result hands back the new id
  and its outline, so a recurring deck is changed with ``edit_presentation``
  instead of generated again and drifting from the approved layout.
* ``search_presentation_text`` - every occurrence of a word or phrase in a
  deck the user can read, with its slide, block address and field path,
  changing nothing. It walks exactly the fields ``replace_text`` would touch
  (``presentation_patch.search_text``), every slide whatever the deck's size,
  so "is the old name still anywhere?" is a read, not a ``replace_text``
  probe the edit tool has to refuse.
* ``delete_presentation`` - the library's ``Delete``, through the same
  service (``presentations.remove_presentation``): the owner's deck only (an
  administrator also a company-managed one), gone for good with its history,
  comments, grants and public link. The description says it is irreversible
  and runs only on the user's explicit request; there is no trash.
* ``list_presentation_comments`` / ``add_presentation_comment`` /
  ``resolve_presentation_comment`` — the review loop: read the
  reviewers' notes with their ids, leave a note or a reply as the user, and
  close a thread once "implement this comment and close it" is done. All three
  run AS THE CALLING USER on ``services/presentation_comments`` — the same
  permission rules as the Comments panel, so an agent never comments where its
  user could not, nor resolves a note its user could not close by hand. The
  "only when the user asks" clause on resolving is carried by the tool's
  description and by the ids the listings surface, not by a system-prompt
  line.
* ``share_presentation`` — change who can open a deck: add, remove or
  replace people (email or user id of an existing account) and groups, at
  ``read_only`` or ``config``. It runs AS THE CALLING USER through the Share
  dialog's own write (``services/presentation_sharing.apply_sharing``): only
  the owner or a ``config`` grantee may share, an address no account matches
  refuses the whole call by name, a company-managed deck is left to admins,
  and every change is one audit row (``via: agent``). ``get_presentation``
  prints the ``Shared with:`` line it answers "who has access?" from.
* ``set_presentation_public_link`` — turn on, regenerate or turn off the link
  anyone may open the deck with, without an account, through
  ``services/presentation_public_links`` like the Share dialog: a manager of
  the deck only (an administrator on a company-managed one), refused while
  the admin switch is off; the result carries the absolute URL. ``pdf_download`` lets its
  visitors also download the PDF of the slides. ``get_presentation``
  says when a public link is on, and whether it offers the PDF.
* ``export_presentation`` — render the deck to a PDF / PPTX file. Offered on
  every turn; whether the user wants a file is the model's call, made on its
  description. Exporting a deck the same turn (or run) already
  exported in that format, with neither the deck nor its template changed
  since, hands back that file instead of rendering it again.

What the agent may and may not author
------------------------------------
The agent produces **structured JSON**, never a page of HTML: every slide picks
one of the controlled layouts in ``app.services.presentation_spec``, and each
layout maps to a fixed React component. Text is a flat inline markup (bold,
emphasis, code, tone spans) parsed into segments — never HTML: the v1 layouts
that carried a paragraph-level ``rich_text`` fragment are retired, and
a v1 slide an agent still emits (a remembered call, a stored deck it read
verbatim) is upgraded to its v2 equivalent by ``validate_spec`` before it is
saved - the result says so. The free-form "arbitrary HTML in a sandboxed
iframe" deck mode is explicitly out of scope.

The ``generate_presentation`` description is BUILT, not written: see
``app.services.presentation_guidance`` — the model-derived spec reference, one
validated example per layout and per block, the tone semantics and the density
budgets.

Persistence is a ``Presentation`` row rather than a ``FileAttachment``: a deck
keeps a stable id across turns so the ``[[PRESENTATION:<id>]]`` marker already
sitting in the conversation stays valid when the deck is edited, and the
frontend reconciles the slide list instead of mounting a second deck.

Like every other tool closure, database work happens on a **dedicated
short-lived session** (never the caller's ``db``): tool calls run mid agentic
loop, after the caller released its pooled connection, and flushing on that
session would pin a connection across every remaining LLM call.
"""

from __future__ import annotations

import copy
import dataclasses
import functools
import hashlib
import inspect
import json
import uuid
from typing import TYPE_CHECKING, Any

from app.core.utils import format_exception_message, safe_uuid
from app.llm.base import AGENT_CHECK_FILE_SOURCE
from app.services.presentation_chrome import (
    footer_status_line,
    opening_subtitle,
    template_chrome_redraws,
)
from app.services.presentation_guidance import (
    ARCHITECTURE_BARE_HINT,
    ARCHITECTURE_EDGE_HINT,
    build_generate_presentation_description,
    build_presentation_reference,
    format_org_chart_guidance,
)
from app.services.presentation_legacy import retired_layout_note
from app.services.presentation_markup import literal_markdown_links, slide_link_targets
from app.services.presentation_notes import (
    NOTES_OPTIONAL_LAYOUTS,
    format_notes_coverage,
    notes_coverage,
)
from app.services.presentation_patch import (
    LAYOUT_DECK_FIELDS,
    MAX_OPERATIONS,
    OPERATIONS,
    ImageSwap,
    PatchResult,
    PresentationPatchError,
    PresentationPatchNoChangeError,
    TextMatch,
    apply_operations,
    canonical,
    coerce_operations,
    coerce_revision,
    describe_operations,
    parse_slide_selection,
    reconcile,
    search_text,
    summarize_patch,
    verify_untouched,
)
from app.services.presentation_pictures import (
    PictureFacts,
    PicturePlacement,
    canonical_file_reference,
    picture_placements,
    slide_file_ids,
    slide_file_references,
)
from app.services.presentation_spec import (
    DASHBOARD_TILE_SIZES,
    DEFAULT_SLIDE_CAP,
    HERO_TONES,
    HERO_VARIANTS,
    MAX_SLIDE_CAP,
    MAX_TITLE_CHARS,
    OVERLAY_VARIANTS,
    PRESENTATION_ASPECT_RATIOS,
    PRESENTATION_THEMES,
    PRESENTATION_TRANSITIONS,
    SHARED_HEROES,
    SLIDE_LAYOUTS,
    SLIDE_PALETTES,
    PresentationSpecError,
    org_chart_hint_note,
    slide_theme_override,
    spec_kind,
    unit_noun,
    validate_spec,
)
from app.services.share_principals import MAX_PRINCIPALS_PER_CALL
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult, WriteReadBack
from app.tools.tool_events import PRESENTATION_SAVED_EVENT, emit_tool_event

from ._shared import TurnExport, export_result, format_file_size, logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.llm.base import FileRef
    from app.models.presentation import Presentation

def _refusal(message: str) -> ToolResult:
    """A call turned down for a validation reason: nothing changed, and *message* says what to do instead.

    ``refused`` keeps it out of the loop's tool-wide failure brake, which
    only runtime errors feed: a model correcting its call is not a tool
    failing, and four corrections must not withdraw every tool of the turn.
    """
    return ToolResult(tool_call_id="", content=message, is_error=True, refused=True)


def _flag(value: object) -> bool:
    """A boolean tool argument, read leniently: ``true`` / ``"true"`` / ``"yes"`` / ``1``."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return value is True or value == 1


def _delete_refusal(presentation: Presentation, tier: str | None) -> str:
    """Why ``delete_presentation`` leaves a deck the user can see but does not own."""
    name = f"**{presentation.title}** (`{presentation.id}`)"
    if presentation.is_company_managed:
        return (
            f"Nothing was deleted: {name} is published to the company, and only an administrator "
            "can delete it."
        )
    if tier == "config":
        return (
            f"Nothing was deleted: {name} is shared with the user for editing, but only its owner "
            "can delete it. Ask the owner, or leave it as it is."
        )
    return (
        f"Nothing was deleted: {name} is shared with the user read-only, and only its owner "
        "can delete it."
    )


#: How many slides one ``capture_presentation_slides`` call may return. Each
#: capture is a full-canvas PNG (1920px on the long edge) forwarded as a
#: high-detail vision block — roughly 2.5k tokens of context apiece — so the
#: cap is what keeps a "show me the deck" reflex from spending a turn's budget
#: on pictures. Three is the review set the deck skills ask for (the opening,
#: the densest slide, the closing); the fourth is slack.
MAX_SLIDE_CAPTURES = 4


def _default_capture_slides(count: int) -> list[int]:
    """0-based indices to capture when the agent names none: first, middle, last.

    A deck is judged at its extremes and at its densest point, and the middle
    slide is the cheapest stand-in for the latter. Shorter decks collapse to
    whatever they have.
    """
    if count <= 0:
        return []
    if count <= 3:
        return list(range(count))
    return [0, count // 2, count - 1]


def _reused_capture(earlier: FileRef, file_context: FileContext | None) -> FileRef:
    """*earlier* handed back again, with the notes the agent saved on it since.

    ``annotate_file`` writes its notes on the ref the turn's context resolved
    for that id, not on the one a capture returned, and the loop quotes the
    notes of the ref a result carries when it withholds pixels it already
    showed.
    """
    # The handler takes ``**kwargs`` unfiltered, so without an injected
    # context this is whatever the model sent under that name.
    current = file_context.get(earlier.file_id) if isinstance(file_context, FileContext) else None
    notes = getattr(current, "notes", None) or earlier.notes
    return dataclasses.replace(earlier, notes=notes)


def _unchanged_capture_line(unchanged: list[int], captured: list[int]) -> str:
    """Say which captured slides render exactly as an earlier capture of this turn."""
    if not unchanged:
        return ""
    listed = ", ".join(str(number) for number in unchanged)
    if len(unchanged) == len(captured):
        return (
            f"\n\nNothing on slide(s) {listed} changed since your earlier capture this turn: "
            "each renders the identical PNG and keeps its file id. If you edited them since, "
            "the edit did not change what they show."
        )
    return (
        f"\n\nSlide(s) {listed} render the identical PNG of your earlier capture this turn "
        "and keep its file id."
    )


#: Per-call budget for the spec JSON returned by ``get_presentation``. A 100-slide
#: deck serializes to a few tens of KB; the budget keeps one oversized deck from
#: eating the whole context window. It is never applied mid-JSON: a spec
#: over budget is served whole slide by whole slide, the tool says which slides
#: are left, and the ``slides`` selector reads any of them in full on the next
#: call — so no slide of a long deck is ever unreadable.
_SPEC_READBACK_BUDGET = 20000

#: Deck-level keys reported next to a partial slide read-back, so the agent
#: sees the theme / ratio / chrome without the whole spec.
_DECK_SETTING_KEYS: tuple[str, ...] = ("title", "theme", "aspect_ratio", "transition", "chrome")

#: ``edit_presentation``'s description. The operation catalog is DERIVED from
#: ``presentation_patch.OPERATION_CONTRACTS`` so an operation added to the
#: engine is documented to the agent the moment it exists — same rule as the
#: spec-derived ``generate_presentation`` description.
_EDIT_DESCRIPTION = (
    "Make a SURGICAL change to an existing deck or page without re-sending it: "
    "one slide, one block, one word. Use this for ANY change to a presentation "
    "that already exists (a word, a title, a bullet, a picture, speaker notes, a "
    "new or removed slide, a reorder, the theme). The operations are applied "
    "server-side to the STORED spec, and every slide no operation "
    "names is guaranteed byte-identical afterwards - the result lists exactly "
    "which slides changed, each with its link: put the link in your reply. "
    "generate_presentation with presentation_id is only "
    "for rewriting a deck wholesale: a re-submitted deck drifts on every slide "
    "the user did not ask about. You do not need the other slides in context: "
    "get_presentation(slides=\"12\") reads the one you change. Address a slide "
    "by its 1-based number or its id (both in every outline); address a block "
    "by position: '3' = block 3; on a columns SLIDE '2.3' = column 2, block 3; "
    "inside a block, '3.2' = 2nd child of callout 3, '3.2.1' = block 3 "
    "(a columns block), column 2, block 1 ('3.2' alone is the column, not a "
    "block), '4.left.1' = comparison 4, left panel, block 1; 'footer' = "
    "footer_block; on a DASHBOARD '2' = panel 2 (its chart or table, and the "
    "panel's title and span). Slide and block "
    "contents follow the generate_presentation contract (same layouts, blocks "
    "and fields); a validation error names the field and nothing is saved. "
    "Several operations in one call are applied in order (positions refer to "
    "the deck as it stands after the previous operation). Pass expected_revision "
    "(the 'revision N' you read) whenever you address a slide by number: a deck "
    "saved since is refused, not edited at the wrong slide. The result carries a "
    "LAYOUT CHECK of the slides you wrote (text cut off, a body shrunk to fit): "
    "fix those by cutting content on that slide, not by touching others. "
    "The BRAND of a deck is its template, its THEME is only the colour scheme "
    "(dark / light): to restyle a deck pass template_id (a UUID from "
    "search_templates, or 'none' to free it), alone or with operations - the "
    "template decides which schemes it supports and the theme snaps to one it "
    "does; setting a theme the deck's template cannot dress is refused. "
    "A slide may carry a theme of its own (update_slide {theme: 'dark'}): it "
    "always wins over the deck theme and a deck theme change leaves it alone; "
    "{theme: null} gives it back to the deck. A title or closing slide takes a "
    "hero backdrop (plain / gradient / split / spotlight, or one of the template's "
    "own covers get_template lists - mosaic / cascade / horizon / pillar), "
    "its hero_tone (brand = the "
    "template's gradient, or one palette colour - accent, gradient_from, ...) and "
    "hero_accent_word; "
    "any slide with a background image takes an overlay (none / soft / strong). "
    "A slide about XTM One takes palette 'xtm_one' (update_slide {palette: 'xtm_one'}, "
    "{palette: null} back to the template's); a content slide with 1-3 images takes "
    "bento: true (3-5 leaf blocks, the first image the large cell). "
    + ARCHITECTURE_BARE_HINT
    + " "
    + ARCHITECTURE_EDGE_HINT
    + " An org chart is changed seat by seat: update_block with the block's whole nodes list, or "
    "replace_block with a new org_chart. "
    + format_org_chart_guidance()
    + "\nOperations:\n"
    + describe_operations()
)

_EDIT_INPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "presentation_id": {
            "type": "string",
            "description": (
                "UUID of the deck to edit (from generate_presentation, "
                "get_presentation or a /presentations/ID link)."
            ),
        },
        "operations": {
            "type": "array",
            "maxItems": MAX_OPERATIONS,
            "description": (
                "Operations applied in order. Each is an object with 'op' plus "
                f"that operation's own keys (see the tool description). At most {MAX_OPERATIONS} "
                "per call. Required unless template_id is given (a template change "
                "alone is a complete edit)."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": list(OPERATIONS)},
                    # One JSON type per property, never a union: Gemini's
                    # function declarations refuse `"type": [...]` and one bad
                    # tool 400s the whole request. A number travels as a digit
                    # string ("12"); the engine reads ints too, for a provider
                    # that ignores the type.
                    "slide": {
                        "type": "string",
                        "description": 'The slide: its 1-based number ("12") or its id.',
                    },
                    "fields": {
                        "type": "object",
                        "description": (
                            "update_slide / update_block / update_deck: the fields to set "
                            "(merged into the target; null removes a field). Only what "
                            "changes."
                        ),
                    },
                    "content": {
                        "type": "object",
                        "description": (
                            "replace_slide / insert_slide: a complete slide object "
                            "(layout + fields); replace_block / insert_block: a complete "
                            "block object (type + fields)."
                        ),
                    },
                    "block": {
                        "type": "string",
                        "description": (
                            "update_block / replace_block / delete_block: block address "
                            "('3', '2.3' on a columns slide, '3.2' a callout child, "
                            "'3.2.1' inside a columns block, '4.left.1', 'footer'); "
                            "replace_image: narrows the swap to that block."
                        ),
                    },
                    "at": {
                        "type": "string",
                        "description": (
                            "insert_slide: 1-based position the new slide takes ('4'; "
                            "default: end). insert_block: block address the new block takes "
                            "('3', '2.1', 'footer'; default: end of the slide's blocks)."
                        ),
                    },
                    "after": {
                        "type": "string",
                        "description": (
                            "insert_slide: put the new slide right after this slide "
                            "(its number or its id)."
                        ),
                    },
                    "to": {
                        "type": "integer",
                        "description": "move_slide: 1-based destination position.",
                    },
                    "find": {
                        "type": "string",
                        "description": (
                            "replace_text: exact text to find (case-sensitive). "
                            "replace_image: the file id of the picture shown now."
                        ),
                    },
                    "replace": {
                        "type": "string",
                        "description": (
                            'replace_text: the replacement ("" deletes the text). '
                            "replace_image: the file id of the new picture."
                        ),
                    },
                    "count": {
                        "type": "integer",
                        "description": (
                            "replace_text / replace_image: the number of occurrences "
                            "(places) you expect; the call is refused if the deck holds "
                            "a different number."
                        ),
                    },
                },
                "required": ["op"],
            },
        },
        "template_id": {
            "type": "string",
            "description": (
                "Restyle the deck with a template: the UUID of a presentation template "
                "(from search_templates) to link, or 'none' to unlink the current one "
                "(renderer theme only). The template's palette, fonts and logos apply "
                "to every slide at once; the deck's theme is kept when the template "
                "supports its scheme and snapped to the template's own scheme "
                "otherwise (stated in the result). Omit to keep the current template."
            ),
        },
        "change_summary": {
            "type": "string",
            "description": (
                "One line for the version history (e.g. 'Fixed the ARR figure on "
                "slide 12'); derived from the operations when omitted."
            ),
        },
        "expected_revision": {
            "type": "integer",
            "description": (
                "The revision you read the slide numbers from (every get_presentation "
                "result, edit result and studio context states 'revision N'). Always "
                "pass it when you address a slide by NUMBER: if the deck was saved "
                "since, the call is refused instead of landing on whatever slide now "
                "holds that number. Not needed when every slide is addressed by id."
            ),
        },
    },
    "required": ["presentation_id"],
}

#: Most root threads ``list_presentation_comments`` returns in one call — a
#: deck under review carries a few dozen notes at most; past this the result
#: says how many more there are rather than flooding the context.
_COMMENT_LIST_LIMIT = 100

#: How the export result names each format.
_EXPORT_LABELS: dict[str, str] = {
    "pdf": "PDF",
    "pptx": "PowerPoint (pixel-perfect slides)",
    "pptx_native": "PowerPoint (editable shapes)",
}


def summarize_slides(spec: dict, *, with_ids: bool = False) -> str:
    """One line per slide: index, layout and title — the agent's edit map.

    Returned after every create/update so the model can see the slide order it
    just produced (and address a slide by index on the next edit) without
    re-reading the whole spec. Public because the Presentation Studio
    (``services/presentation_studio.py``) puts the same outline in the system
    prompt of a conversation bound to a deck — one shape for the edit map,
    whether the agent read it from a tool result or from its context.

    ``with_ids`` appends each slide's stable id (``id=…``): tool results carry
    it so ``edit_presentation`` can address a slide by identity across inserts
    and reorders; the studio's system prompt leaves it out — positions
    are enough there and the block is paid on every turn.
    """
    lines = []
    # A page (portrait document) is read, not presented: no speaker view, so
    # no notes to owe.
    spoken = spec_kind(spec) == "deck"
    for idx, slide in enumerate(spec.get("slides") or []):
        if not isinstance(slide, dict):
            continue
        title = slide.get("title") or slide.get("subtitle") or "(untitled)"
        # A content slide the presenter has nothing to say over is flagged in
        # the outline itself: the agent edits from this map, so the
        # gap is visible where the slide is addressed, not only in the
        # coverage line under it.
        notes = slide.get("notes")
        missing = (
            spoken
            and slide.get("layout") not in NOTES_OPTIONAL_LAYOUTS
            and not (isinstance(notes, str) and notes.strip())
        )
        flag = " (no notes)" if missing else ""
        # A slide read in its own scheme is marked where it is
        # addressed: a deck theme change will not reach it, and the agent
        # must know that before it promises a light deck.
        own_theme = slide_theme_override(slide)
        scheme = f" [theme: {own_theme}]" if own_theme else ""
        ident = f"  id={slide.get('id')}" if with_ids and slide.get("id") else ""
        lines.append(f"  {idx + 1}. [{slide.get('layout')}] {title}{scheme}{flag}{ident}")
    return "\n".join(lines)


def _format_positions(indices: list[int]) -> str:
    """0-based *indices* as the 1-based ranges a person writes: ``16-25, 30``."""
    if not indices:
        return ""
    ordered = sorted(set(indices))
    ranges: list[str] = []
    start = previous = ordered[0]
    for index in ordered[1:]:
        if index == previous + 1:
            previous = index
            continue
        ranges.append(f"{start + 1}-{previous + 1}" if start != previous else f"{start + 1}")
        start = previous = index
    ranges.append(f"{start + 1}-{previous + 1}" if start != previous else f"{start + 1}")
    return ", ".join(ranges)


def _slide_payloads(spec: dict, indices: list[int], budget: int) -> tuple[list[str], list[int]]:
    """Whole-slide JSON dumps for *indices* that fit in *budget*, plus the indices left out.

    Slides are served in the order asked and never cut: the first slide is
    always included (a single slide over budget is still one readable slide),
    then whole slides are added while they fit, and everything from the first
    slide that does not fit onwards is reported as left — so "what remains" is
    always a contiguous tail the agent can ask for by range.
    """
    slides = spec.get("slides") or []
    shown: list[str] = []
    left: list[int] = []
    used = 0
    for position, index in enumerate(indices):
        slide = slides[index]
        payload = json.dumps(slide, ensure_ascii=False, separators=(",", ":"))
        if shown and used + len(payload) > budget:
            left = list(indices[position:])
            break
        header = f"--- slide {index + 1} [{slide.get('layout')}] id={slide.get('id')} ---"
        shown.append(f"{header}\n{payload}")
        used += len(payload)
    return shown, left


def _deck_settings(spec: dict) -> str:
    """The deck-level keys as compact JSON, for a partial read-back."""
    settings = {key: spec[key] for key in _DECK_SETTING_KEYS if key in spec}
    return json.dumps(settings, ensure_ascii=False, separators=(",", ":"))


#: Most files one picture list describes. A logo wall is a dozen files,
#: so a slide or two of one is always described whole. Past the cap, a list
#: spread over several slides names the slides whose pictures it left out
#: (reading fewer of them lists them); a list of ONE slide, where reading fewer
#: slides cannot help, names the rest by id and place.
_PICTURE_LIST_FILES = 16
#: The cap when every listed picture is on one slide: two full logo walls.
_PICTURE_LIST_FILES_ONE_SLIDE = 32
#: Most places listed under one file; the rest are counted.
_PICTURE_LIST_PLACES = 6
#: Most places an edit account names per swap; the rest are counted.
_PICTURE_SWAP_PLACES = 12
#: How much of an asset's caption (or an agent's saved notes) the list quotes.
_PICTURE_CAPTION_CHARS = 120
#: How much of a filename or a template name a line prints.
_PICTURE_NAME_CHARS = 80
#: Most places a result names for the pictures a write left empty; the rest are counted.
_PICTURE_GONE_PLACES = 6


def _clip(text: str | None, limit: int) -> str:
    value = " ".join((text or "").split()).replace('"', "'")
    if len(value) > limit:
        value = value[: limit - 3].rstrip() + "..."
    return value


def _quote(text: str | None, limit: int = _PICTURE_CAPTION_CHARS) -> str:
    value = _clip(text, limit)
    return f'"{value}"' if value else ""


def _named_places(places: list[str], limit: int) -> str:
    named = "; ".join(places[:limit])
    return f"{named}; and {len(places) - limit} more" if len(places) > limit else named


def picture_name(facts: PictureFacts | None) -> str:
    """One file named in passing: its filename, what it is when the name is
    not this reader's (:attr:`PictureFacts.withheld`), or that it is gone."""
    if facts is None:
        return "missing file"
    if not facts.withheld:
        return _clip(facts.filename, _PICTURE_NAME_CHARS)
    if facts.origin == "template asset":
        return "an asset of a template you cannot open"
    return f"a file {facts.origin} by another user"


def picture_facts_line(facts: PictureFacts | None) -> str:
    """What one file is, in one line - or that it no longer exists.

    A file whose name is not this reader's is told as what it is (an asset of
    a template they cannot open, a file another user uploaded or generated),
    never by a name or notes they could not read anywhere else.
    """
    if facts is None:
        return "MISSING - no platform file has this id, so nothing is drawn there"
    if facts.origin == "template asset":
        role = f" [{facts.role}]" if facts.role else ""
        if facts.template_name:
            template = _clip(facts.template_name, _PICTURE_NAME_CHARS)
            kind = f'template asset{role} of "{template}" (template_id `{facts.template_id}`)'
        elif facts.withheld:
            kind = f"template asset{role} of a template you cannot open"
        else:
            kind = f"template asset{role}"
    elif facts.withheld:
        kind = picture_name(facts)
    else:
        kind = facts.origin
    name = "" if facts.withheld else f"{_clip(facts.filename, _PICTURE_NAME_CHARS)}, "
    caption = _quote(facts.description)
    added = f", added {facts.added.isoformat()}" if facts.added else ""
    return f"{name}{kind}{': ' + caption if caption else ''} - {format_file_size(facts.size)}{added}"


def format_picture_list(
    placements: list[PicturePlacement],
    facts: dict[str, PictureFacts],
    *,
    spec: dict,
    scope: str,
    editable: bool = False,
    template_id: str | None = None,
    read_hint: bool = True,
) -> str:
    """The pictures of the slides a result serves, grouped by file, or ``""``.

    Each file with what it is (``picture_facts_line``) and every place it is
    shown, in ``edit_presentation``'s address grammar: the handle a
    ``replace_image`` takes and the answer to "which one is the old
    screenshot?". A deck's pictures are template assets or files of the
    conversation that built it, which ``read_file_content`` does not open -
    the list says so, and where a replacement is picked.
    """
    if not placements:
        return ""
    unit = unit_noun(spec, 1)
    by_file: dict[str, list[PicturePlacement]] = {}
    for placement in placements:
        by_file.setdefault(placement.key, []).append(placement)
    one_slide = len({placement.slide_index for placement in placements}) == 1
    cap = _PICTURE_LIST_FILES_ONE_SLIDE if one_slide else _PICTURE_LIST_FILES
    files = list(by_file.items())
    lines = [f"Pictures on {scope} ({len(by_file)} file(s), {len(placements)} place(s)):"]
    for file_id, where in files[:cap]:
        places = _named_places([placement.label(unit) for placement in where], _PICTURE_LIST_PLACES)
        lines.append(f"- `{file_id}`: {picture_facts_line(facts.get(file_id))}\n    shown at {places}")
    rest = files[cap:]
    if rest and one_slide:
        # Reading fewer slides cannot help: every id is named, with its place.
        for file_id, where in rest:
            places = _named_places([placement.label(unit) for placement in where], 1)
            lines.append(f"- `{file_id}`: shown at {places}")
    elif rest:
        holding = sorted({placement.slide_index + 1 for _file_id, where in rest for placement in where})
        numbers = ", ".join(str(number) for number in holding)
        lines.append(
            f"- and {len(rest)} more file(s), on {unit_noun(spec, len(holding))} {numbers}: "
            f'get_presentation(slides="{holding[0]}") lists every picture of one {unit}.'
        )
    if read_hint:
        lines.append(
            "read_file_content opens only files attached to this conversation; "
            f"capture_presentation_slides shows these {unit}s as rendered."
        )
    if editable:
        roles = {
            facts[key].role
            for key in by_file
            if key in facts and facts[key].origin == "template asset" and facts[key].role
        }
        pick_args = f'template_id="{template_id}"' if template_id else ""
        if len(roles) == 1:
            pick_args += f'{", " if pick_args else ""}role="{next(iter(roles))}"'
        lines.append(
            'To swap one at every place it is shown: edit_presentation {"op": "replace_image", '
            '"find": "FILE_ID", "replace": "NEW_FILE_ID", "count": PLACES} (slide / block '
            f"narrow it). A new picture: get_template({pick_args}) lists the template's assets "
            "with their file ids (query= narrows it), or use a file attached to this conversation."
        )
    return "\n".join(lines)


def picture_swaps_line(
    swaps: list[ImageSwap],
    facts: dict[str, PictureFacts] | None,
    *,
    described: set[str] | None = None,
) -> str:
    """The ``edit_presentation`` account of its ``replace_image`` operations.

    *facts* ``None`` means the files could not be looked up, and an id outside
    *described* (``None``: every id) was deliberately not looked up:
    either way the swap is told by id alone rather than calling a file that
    exists "missing".
    """
    if not swaps:
        return ""
    count = sum(len(swap.places) for swap in swaps)
    lines = [f"Pictures swapped ({count} place{'s' if count != 1 else ''}):"]

    def _described(file_id: str) -> bool:
        return described is None or file_id in described

    for swap in swaps:
        old_text = new_text = ""
        if facts is not None:
            if _described(swap.find):
                old_text = f" ({picture_name(facts.get(swap.find))})"
            if _described(swap.replace):
                new_text = f" ({picture_facts_line(facts.get(swap.replace))})"
        places = _named_places(swap.places, _PICTURE_SWAP_PLACES)
        lines.append(f"  `{swap.find}`{old_text} -> `{swap.replace}`{new_text}\n    at {places}")
    shown = list(dict.fromkeys(index for swap in swaps for index in swap.slides))
    if shown:
        numbers = ",".join(str(index) for index in shown[:MAX_SLIDE_CAPTURES])
        lines.append(f'capture_presentation_slides(slides="{numbers}") shows them as rendered.')
    return "\n".join(lines)


async def load_deck_picture_facts(
    db: AsyncSession,
    spec: dict,
    *,
    user_id: uuid.UUID | None,
    group_ids: list[str] | None = None,
) -> dict[str, PictureFacts]:
    """What every picture a deck places is, or ``{}`` for a deck without one (no query)."""
    ids = {value for _path, _layout, value in slide_file_references(spec)}
    if not ids:
        return {}
    from app.services.presentation_pictures import load_picture_facts

    return await load_picture_facts(db, ids, user_id=user_id, group_ids=group_ids)


def gone_pictures_line(spec: dict, gone: list[tuple[str, str | None, str]]) -> str:
    """The pictures a save kept although their files no longer exist, or ``""``.

    A picture the stored deck already showed whose file is gone - a removed
    built-in template's asset - no longer refuses the write of its slide: the
    write is saved as it is and nothing is drawn in that place. The result names
    the places so the agent can tell the user, and replace one (``replace_image``)
    or delete its block when asked.
    """
    if not gone:
        return ""
    paths = {path for path, _layout, _value in gone}
    unit = unit_noun(spec, 1)
    places = [
        placement.label(unit)
        for placement, (path, _layout, _value) in zip(picture_placements(spec), slide_file_references(spec), strict=True)
        if path in paths
    ]
    count = len({canonical_file_reference(value) for _path, _layout, value in gone})
    what = "picture is" if count == 1 else "pictures are"
    return (
        f"{count} {what} no longer on the platform (the file was removed), so nothing is drawn at: "
        f"{_named_places(places, _PICTURE_GONE_PLACES)}. Saved anyway; replace_image swaps one, "
        "or delete its block, when the user asks."
    )


def slide_link(deck_id: str, slide_id: str) -> str:
    """The address of one slide: ``/presentations/<deck>#/<slide id>``.

    Anchored on the slide's STABLE id, never its position, so the link still
    lands on the same slide after a colleague inserts or reorders slides; the
    id grammar (``SlideId``) is URL-fragment safe by construction. Relative on
    purpose: in the web chat the link moves the deck already on screen (the
    studio stage, the inline card) to that slide, and the viewer opens on it
    anywhere else - the same fragment ``parseSlideHash`` reads. External
    channels get their absolute link from the marker, not from this text.
    """
    return f"/presentations/{deck_id}#/{slide_id}"


def focus_marker(deck_id: str, slide_id: str | None) -> str:
    """The deck marker, carrying the slide the chat should land on when there is one."""
    if slide_id:
        return f"[[PRESENTATION:{deck_id}#/{slide_id}]]"
    return f"[[PRESENTATION:{deck_id}]]"


def focus_slide_id(outcome: PatchResult) -> str | None:
    """The slide an edit points the user at: the first it wrote, else the first it moved.

    A deletion, or a change to a deck-level field (title, theme), rewrote no
    slide the user could be sent to - the deck as a whole is what changed - so
    there is nothing to focus and the plain marker is used.
    """
    for refs in (outcome.changed, outcome.inserted):
        if refs:
            return refs[0].id
    if outcome.moved:
        return outcome.moved[0][0].id
    return None


def _render_edit_account(
    spec: dict,
    *,
    deck_id: str,
    revision: int,
    previous_revision: int,
    outcome: PatchResult,
    summary: str,
    layout_note: str = "",
    template_line: str = "",
    template_summary: str = "",
    theme_note: str = "",
    deck_theme_line: str = "",
    footer_line: str = "",
    upgrade_note: str = "",
    pictures_line: str = "",
) -> str:
    """The ``edit_presentation`` result: exactly what changed, and the proof of what did not."""
    slides = spec.get("slides") or []
    changed_count = len(outcome.changed)
    lines = [
        f"Updated {_describe(spec)} **{spec.get('title')}** ({_count(spec)}, revision {revision}): "
        f"{changed_count} {unit_noun(spec, changed_count)} changed, "
        f"{outcome.unchanged_count} unchanged - verified byte-identical to revision {previous_revision}."
    ]
    # A slide the operations wrote in a retired v1 shape was upgraded
    # before it was saved: said first, so the agent stops emitting the shape.
    if upgrade_note:
        lines.append(upgrade_note)
    # The template change comes first: it is what the user sees on
    # every slide at once, and the line says which schemes the deck may now
    # be read in - the constraint behind any theme change the user asks next.
    if template_summary:
        lines.append(f"{template_summary} - every {unit_noun(spec, 1)} is restyled, none was rewritten.")
    if template_line:
        lines.append(template_line)
    if theme_note:
        lines.append(theme_note)
    if deck_theme_line:
        lines.append(deck_theme_line)
    # What the footer now READS, whenever the edit could have changed
    # it: the agent quotes it instead of guessing, and learns that the next
    # footer request is one chrome.fields value, never a pass over the slides.
    if footer_line:
        lines.append(footer_line)
    # Every slide the edit wrote or moved is named WITH its address:
    # the model composes its reply from this account, and "change applied,
    # see slide 4" needs the link where the slide is named, not a form to
    # assemble from an id three lines down.
    if outcome.changed:
        lines.append("Changed:")
        lines.extend(
            f"  {ref.index}. [{ref.layout}] {ref.title}  link: {slide_link(deck_id, ref.id)}"
            for ref in outcome.changed
        )
    if outcome.inserted:
        lines.append("Inserted:")
        lines.extend(
            f"  {ref.index}. [{ref.layout}] {ref.title}  link: {slide_link(deck_id, ref.id)}"
            for ref in outcome.inserted
        )
    if outcome.deleted:
        lines.append(
            "Deleted: "
            + "; ".join(f"former {unit_noun(spec, 1)} {ref.index} [{ref.layout}] {ref.title}" for ref in outcome.deleted)
        )
    if outcome.moved:
        lines.append(
            "Moved: "
            + "; ".join(
                f"{unit_noun(spec, 1)} {old} -> {ref.index} [{ref.layout}] {ref.title}"
                f"  link: {slide_link(deck_id, ref.id)}"
                for ref, old in outcome.moved
            )
        )
    focus = focus_slide_id(outcome)
    if focus:
        first = next(
            ref
            for ref in (*outcome.changed, *outcome.inserted, *(ref for ref, _old in outcome.moved))
            if ref.id == focus
        )
        noun = unit_noun(spec, 1)
        lines.append(
            f"Point the user at the change: put the {noun}'s link in your reply, e.g. "
            f"[{noun} {first.index}]({slide_link(deck_id, focus)}) - in the chat it takes "
            f"them straight to that {noun}."
        )
    if outcome.deck_fields:
        lines.append(f"Deck fields changed: {', '.join(outcome.deck_fields)}")
    if outcome.named_unchanged:
        lines.append(
            "Already as requested (named, unchanged): "
            + ", ".join(f"{unit_noun(spec, 1)} {ref.index}" for ref in outcome.named_unchanged)
        )
    if outcome.replacements:
        lines.append(f"Text replacements: {outcome.replacements}")
    if pictures_line:
        lines.append(pictures_line)
    lines.append(f'History: "{summary}"')
    if slides:
        lines.append(f"Current {unit_noun(spec, len(slides))}:\n{summarize_slides(spec, with_ids=True)}")
    notes = notes_line(spec) if spec_kind(spec) == "deck" else ""
    if notes:
        lines.append(notes)
    links = slide_links_line(spec)
    if links:
        lines.append(links)
    if layout_note:
        lines.append(layout_note)
    # The marker carries the focus too: the collapsed card in the studio names
    # the slide and offers to go there, and the inline card opens on it.
    lines.append(
        f"presentation_id: `{deck_id}` — keep editing it with edit_presentation "
        "(what the user sees updates in place).\n"
        f"{focus_marker(deck_id, focus)}"
    )
    return "\n".join(lines)


def notes_line(spec: dict) -> str:
    """The ``Speaker notes:`` coverage line for a tool result (``""`` for an empty deck)."""
    return format_notes_coverage(notes_coverage(spec.get("slides") or []))


#: Broken slide links one tool result names; the count is always complete.
_BROKEN_SLIDE_LINKS_LISTED = 12


def _strings_in(node: object):
    """Every string inside *node* (a slide, a block), depth first."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings_in(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings_in(value)


def slide_links_line(spec: dict) -> str:
    """The ``Slide links:`` line of a save: how many, and the ones that are not links.

    A ``[text](#/<slide id>)`` link names its target by id, so a reorder never
    breaks it; what does is a target that is not (or no longer) a slide of the
    deck - a typo, a position written where the id belongs, a deleted slide.
    A Markdown link to anything but a slide, an ``https://`` page or a
    ``mailto:`` address (``[x](http://...)``, ``[x](/path)``) is not read at
    all and shows on the slide as its raw text. Both are saved as they stand;
    this line is how the agent learns, in the same turn, which to fix.
    ``""`` when the deck has neither a slide link nor such a text.
    """
    slides = [slide for slide in spec.get("slides") or [] if isinstance(slide, dict)]
    ids = {slide.get("id") for slide in slides if isinstance(slide.get("id"), str)}
    noun = unit_noun(spec, 1)
    total = 0
    broken: list[str] = []
    literal: list[str] = []
    for position, slide in enumerate(slides, start=1):
        for value in _strings_in(slide):
            for target in slide_link_targets(value):
                total += 1
                if target not in ids:
                    broken.append(f"{noun} {position} -> #/{target}")
            literal.extend(f"{noun} {position}: {_clip(text, 80)}" for text in literal_markdown_links(value))
    parts: list[str] = []
    if total:
        links = "link" if total == 1 else "links"
        if not broken:
            parts.append(f"Slide links: {total} {links}, every one lands on a {noun} of this deck.")
        else:
            parts.append(
                f"Slide links: {total} {links}, {len(broken)} pointing at no {noun} of this deck "
                f"({_listed(broken)}) - drawn as plain text. A link names its target by the "
                f"{noun}'s id (the ids get_presentation lists), never by its number."
            )
    if literal:
        parts.append(
            f"Not links: {len(literal)} Markdown link{'' if len(literal) == 1 else 's'} shown as raw text "
            f"({_listed(literal)}). A link points at a {noun} (#/SLIDE_ID), an https:// page or a "
            "mailto: address."
        )
    return "\n".join(parts)


def _listed(places: list[str]) -> str:
    """At most :data:`_BROKEN_SLIDE_LINKS_LISTED` places, then how many more."""
    listed = ", ".join(places[:_BROKEN_SLIDE_LINKS_LISTED])
    more = len(places) - _BROKEN_SLIDE_LINKS_LISTED
    return f"{listed} and {more} more" if more > 0 else listed


def _describe(spec: dict) -> str:
    """``"presentation"`` for a slide deck, ``"A4 page"`` / ``"letter page"`` for a
    portrait document — the noun a tool result opens with.

    A product brief reported back as a "presentation (2 slides)" would have the
    agent tell the user it made a slide show; the tool speaks the reader's
    vocabulary so the model does too.
    """
    if spec_kind(spec) == "page":
        return f"{spec.get('aspect_ratio')} page"
    return "presentation"


def _count(spec: dict) -> str:
    """``"6 slides"`` / ``"2 pages"`` / ``"1 page"``."""
    count = len(spec.get("slides") or [])
    return f"{count} {unit_noun(spec, count)}"


#: Longest query ``search_presentation_text`` takes: a phrase, not a paragraph.
MAX_SEARCH_QUERY_CHARS = 500

#: Occurrences one ``search_presentation_text`` result lists; the count and
#: the slides they are on are always complete.
_SEARCH_LIST_LIMIT = 60

#: What a text search covers, in the words the result and the description use.
_SEARCHED_FIELDS = "titles, blocks, table cells, chart labels and captions, speaker notes, header and footer"


def _search_mode(*, case_sensitive: bool, whole_word: bool) -> str:
    mode = "case-sensitive" if case_sensitive else "case-insensitive"
    return f"{mode}, whole word" if whole_word else mode


def _match_place(spec: dict, match: TextMatch) -> str:
    if match.slide is None:
        return f"deck {match.field}"
    place = f"{unit_noun(spec, 1)} {match.slide} [{match.layout}] id={match.slide_id}"
    if match.block is not None:
        place += f", block {match.block}"
    return f"{place}, {match.field}" if match.field else place


def format_text_search(
    spec: dict,
    matches: list[TextMatch],
    query: str,
    *,
    revision: int,
    case_sensitive: bool,
    whole_word: bool,
) -> str:
    """The ``search_presentation_text`` result: every occurrence, or a clear "none".

    Each line gives the slide (position, layout, id), the block address
    ``edit_presentation`` takes and the field path inside it, then the stored
    text around the match with the match itself between « » - exactly as
    stored, which is what ``replace_text`` must be given. A zero result says
    what a looser search would find, so the next call is the right one.
    """
    mode = _search_mode(case_sensitive=case_sensitive, whole_word=whole_word)
    head = (
        f"Searched {_describe(spec)} **{spec.get('title')}** (revision {revision}, {_count(spec)}; "
        f"every text field: {_SEARCHED_FIELDS}) for '{query}' ({mode})."
    )
    if not matches:
        looser = []
        if case_sensitive:
            found = len(search_text(spec, query, case_sensitive=False, whole_word=whole_word))
            if found:
                looser.append(f"a case-insensitive search finds {found}")
        if whole_word:
            found = len(search_text(spec, query, case_sensitive=case_sensitive, whole_word=False))
            if found:
                looser.append(f"without whole_word it occurs {found} time(s), inside longer words")
        hint = f" ({'; '.join(looser)})" if looser else ""
        return f"{head}\nNo occurrence{hint}: the text does not appear anywhere. Nothing was changed."
    slides = sorted({m.slide - 1 for m in matches if m.slide is not None})
    deck_fields = sorted({m.field for m in matches if m.slide is None})
    where = []
    if slides:
        where.append(f"on {unit_noun(spec, len(slides))} {_format_positions(slides)}")
    if deck_fields:
        where.append(f"in the deck's {', '.join(deck_fields)}")
    lines = [
        f"{head}\n{len(matches)} occurrence(s), {' and '.join(where)}. Nothing was changed.",
        *(f"- {_match_place(spec, m)}: \"{m.snippet}\"" for m in matches[:_SEARCH_LIST_LIMIT]),
    ]
    if len(matches) > _SEARCH_LIST_LIMIT:
        lines.append(
            f"... and {len(matches) - _SEARCH_LIST_LIMIT} more, not listed. Narrow the query, or "
            "turn on case_sensitive / whole_word, to list them."
        )
    lines.append(
        "To change one, call edit_presentation: replace_text with find = the text between « » "
        "exactly as shown (matching there is exact and case-sensitive) and slide = its id, or "
        "update_block / update_slide on the block and field named."
    )
    return "\n".join(lines)


def template_status_line(
    template_name: str | None, supported_schemes: object, *, note: str | None = None
) -> str:
    """The ``Template:`` line of a tool result.

    Names the template that brands the deck and the colour schemes it can
    dress it in — the constraint the agent must respect before offering a
    theme change — or says the deck is free (renderer theme only, both
    schemes, restyle it with ``template_id``). *note* states HOW the template
    was chosen when it was not the agent's explicit ``template_id`` (the
    platform default of an automation, the only template the user has), so the
    agent tells the user rather than presenting a default as a decision.
    """
    if not template_name:
        line = "Template: none (free deck - renderer theme only, dark or light; link one with template_id)"
    else:
        schemes = (
            [str(s) for s in supported_schemes]
            if isinstance(supported_schemes, (list, tuple))
            else []
        )
        if not schemes:
            line = f"Template: {template_name}"
        else:
            only = " only" if len(schemes) == 1 else ""
            line = f"Template: {template_name} (themes: {' / '.join(schemes)}{only})"
    return f"{line} - {note}" if note else line


#: The read-only tool id every deck agent also carries: a template is
#: chosen by name, so an agent that can create a deck must be able to list the
#: templates the user may pick from and read their predefined assets.
DECK_COMPANION_TOOL_IDS: tuple[str, ...] = ("template_search",)


def ensure_deck_tools(builtin_tool_ids: list[str] | None) -> list[str]:
    """The turn's built-in tool ids, with ``template_search`` appended when they carry ``generate_presentation``.

    Order is preserved and nothing is removed; idempotent. An agent configured
    with the deck tool alone could otherwise be asked "which template?" by the
    tool and have no way to answer it - the templates are listed by
    ``search_templates``, and the assets a deck places by ``get_template``.
    """
    resolved = list(builtin_tool_ids or [])
    if "generate_presentation" not in resolved:
        return resolved
    for tool_id in DECK_COMPANION_TOOL_IDS:
        if tool_id not in resolved:
            resolved.append(tool_id)
    return resolved


def is_interactive_turn(conversation_id: str | None, assignment_run_id: str | None) -> bool:
    """Whether the tools serve a turn where a person can be asked a question.

    A chat turn (a conversation, no run behind it) is interactive: the agent
    can ask the user which template to use before creating a deck. An
    assignment run, or a chat turn delegated from one (``assignment_run_id`` is
    the parent run), has nobody to ask - it takes the platform default, as it
    always did.
    """
    return bool(conversation_id) and not assignment_run_id


async def layout_check_note(
    deck_id: uuid.UUID,
    user_uuid: uuid.UUID | None,
    spec: dict,
    *,
    revision: int | None = None,
    written_slide_ids: set[str] | None = None,
) -> str:
    """The ``Layout check`` lines of a tool result, or ``""``.

    Runs the saved deck through the headless render and reports what the
    audience would see go wrong (``services/presentation_layout.py``). It is a
    courtesy the save must never depend on: any failure is logged and the
    result simply carries no layout section. *revision* is the one *spec* was
    read from (or just saved as): the check is pinned to it, so a collaborator's
    save landing mid-render is a skipped check rather than a report about a deck
    the agent does not hold, and a read-back of the same revision reuses the
    report the save produced instead of rendering again. Module-level so the
    tests can replace it - a unit test of the tool has no browser to render with.

    With *written_slide_ids* (the surgical edit) the report is narrowed
    to those slides: the agent learns what ITS change did to the slide it
    touched, and only how many pre-existing findings sit on the slides it did
    not — fixing those unasked would be exactly the deck-wide rewrite the edit
    tool exists to prevent. An EMPTY set (the edit rewrote no slide content: a
    move, a delete) measures nothing — no slide's rendering changed, so there
    is no finding that could be this edit's consequence; the caller passes
    ``None`` for a deck-level change, which re-renders every slide.
    """
    if user_uuid is None:
        return ""
    if written_slide_ids is not None and not written_slide_ids:
        return ""
    from app.services.presentation_layout import (
        check_presentation_layout,
        format_layout_report,
        narrow_layout_report,
    )

    try:
        report = await check_presentation_layout(deck_id, user_uuid, spec, expected_revision=revision)
    except Exception as exc:
        logger.warning(
            "Presentation layout check failed for %s: %s",
            deck_id,
            format_exception_message(exc),
        )
        return ""
    if written_slide_ids is None:
        return format_layout_report(report)

    indexes = {
        index
        for index, slide in enumerate(spec.get("slides") or [])
        if isinstance(slide, dict) and slide.get("id") in written_slide_ids
    }
    narrowed, elsewhere = narrow_layout_report(report, slide_ids=written_slide_ids, slide_indexes=indexes)
    unit = narrowed.unit or "slide"
    if narrowed.skipped or narrowed.findings:
        note = format_layout_report(narrowed)
    else:
        count = narrowed.slide_count
        note = (
            f"Layout check: the {unit} this edit wrote fits its canvas."
            if count == 1
            else f"Layout check: the {count} {unit}s this edit wrote fit their canvas."
        )
    if note and elsewhere and not narrowed.skipped:
        note += (
            f"\nNot listed: {elsewhere} pre-existing finding(s) on {unit}s this edit did not "
            "touch - unchanged by it (get_presentation or the Layout panel lists them)."
        )
    return note


async def resolve_presentation_id_prefix(db: Any, value: object, user_id: str) -> tuple[object, str | None]:
    """*value* with an id prefix (8+ hexadecimal characters) of one deck the
    user may read taken for its full id, under ``load_visible_presentation``'s
    own visibility (``user_visible_filter``, no admin bypass); the refusal
    naming the candidates when it starts several. A full UUID, anything that is
    not an id prefix and a prefix starting no deck come back unchanged."""
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.models.presentation import Presentation
    from app.services.builtin_tools.id_prefix import id_prefix, resolve_id_prefix

    user_uuid = safe_uuid(user_id)
    if id_prefix(value) is None or user_uuid is None:
        return value, None
    groups = await get_user_group_ids(db, user_uuid)
    visible = user_visible_filter(Presentation, user_uuid, groups)
    return await resolve_id_prefix(
        db, Presentation, value, [visible], noun="presentations", label=Presentation.title
    )


def _with_presentation_id_prefix(handler: Any, user_id: str) -> Any:
    """*handler* taking a ``presentation_id`` that is an id prefix of one deck
    the user may read for its full id (:func:`resolve_presentation_id_prefix`);
    a prefix starting several decks is refused naming them before the handler
    runs. A full id, an empty one or a prefix starting none reaches the handler
    unchanged, so it answers its usual "no presentation you can access"."""
    from app.services.builtin_tools.id_prefix import id_prefix

    @functools.wraps(handler)
    async def resolved(*args: Any, **kwargs: Any) -> Any:
        value = kwargs.get("presentation_id")
        if id_prefix(value) is not None:
            from app.core.database import async_session

            async with async_session() as session:
                value, ambiguous = await resolve_presentation_id_prefix(session, value, user_id)
            if ambiguous:
                return ambiguous
            kwargs["presentation_id"] = value
        return await handler(*args, **kwargs)

    return resolved


def build_presentation_tools(
    db: AsyncSession,
    user_id: str,
    conversation_id: str | None = None,
    *,
    assignment_run_id: str | None = None,
    request_text: str | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build the deck generation + read-back tools for an agent.

    ``request_text`` is the text the agent is acting on this turn — the chat
    message (a sub-agent task's description on the delegated path), the
    rendered prompt of an assignment run: the template choice of a new deck
    reads it.
    """
    del db  # every operation opens its own short-lived session
    # The files this turn's (or run's) exports stored, by deck, revision,
    # format and resolved template: a deck nobody changed since its export is
    # the same file.
    exported_files: dict[tuple[uuid.UUID, int, str, str], TurnExport] = {}
    # Whether a NEW deck's template may be asked of the user: a chat
    # turn can, an assignment run (or a turn delegated from one) cannot.
    interactive = is_interactive_turn(conversation_id, assignment_run_id)
    # The slide PNGs this turn's captures stored, by deck, 0-based slide
    # position and content digest. The loop shows an image's pixels once per
    # file per turn and counts the distinct images it showed against the
    # turn's vision budget, so a re-capture of a slide that renders
    # the same pixels must hand back the same file.
    captured_slides: dict[tuple[uuid.UUID, int, str], FileRef] = {}

    async def _generate_presentation(
        title: str,
        slides: object,
        theme: str | None = None,
        aspect_ratio: str | None = None,
        transition: str | None = None,
        template_id: str | None = None,
        presentation_id: str | None = None,
        chrome: object = None,
        change_summary: str | None = None,
        **_kw: object,
    ) -> ToolResult:
        from app.core.database import async_session
        from app.services.presentations import (
            DECK_SLIDE_DEFAULT_KEYS,
            TemplateChoiceError,
            apply_deck_defaults,
            deck_heroes,
            fit_slide_heroes,
            fit_slide_themes_to_schemes,
            fit_theme_to_schemes,
            format_template_choice,
            list_template_options,
            load_visible_presentation,
            load_visible_template_facts,
            load_writable_presentation,
            mark_template_choice_asked,
            resolve_deck_template_id,
            save_presentation,
            slide_hero_snap_note,
            slide_theme_snap_note,
            template_choice_asked,
            template_choice_settled,
            theme_fits_schemes,
        )

        # LLMs occasionally hand a JSON *string* to an array-typed parameter.
        # Parsing it here turns a guaranteed failure into a working call; a
        # non-JSON string still lands in the normal validation error below.
        if isinstance(slides, str):
            try:
                slides = json.loads(slides)
            except ValueError:
                return _refusal(
                    "slides must be a JSON array of slide objects, each with "
                    f"a 'layout' key (one of: {', '.join(SLIDE_LAYOUTS)})."
                )

        user_uuid = safe_uuid(user_id)
        if user_uuid is None:
            # Anonymous external-channel callers have no user row to own the
            # deck, and a deck is only reachable through the authenticated
            # read endpoint — so there would be nothing to render.
            return _refusal(
                "Presentations can only be generated for a signed-in user. "
                "Produce the content as a document via generate_file instead."
            )

        target_id = None
        if presentation_id:
            target_id = safe_uuid(str(presentation_id).strip())
            if target_id is None:
                return _refusal(
                    "presentation_id must be the UUID of a deck you generated "
                    "earlier (omit it to create a new deck)."
                )

        if isinstance(chrome, str):
            try:
                chrome = json.loads(chrome)
            except ValueError:
                return _refusal(
                    "chrome must be an object: {show_logo, logo_position, show_footer, "
                    "show_slide_numbers, event_label, footer_text, fields}."
                )

        # Only the keys the agent actually set: ``PresentationSpec`` owns the
        # defaults, and the template's ``deck_defaults`` slot in between the
        # agent's explicit choice and the model default.
        spec_payload: dict = {"title": title, "slides": slides}
        for key, value in (
            ("theme", theme),
            ("aspect_ratio", aspect_ratio),
            ("transition", transition),
        ):
            if isinstance(value, str) and value.strip():
                spec_payload[key] = value.strip()
        if isinstance(chrome, dict) and chrome:
            spec_payload["chrome"] = chrome

        try:
            async with async_session() as own_db:
                existing = None
                if target_id is not None:
                    # Writable = the user's own deck, or one shared with them
                    # for editing — the contract that lets a colleague
                    # keep working on a deck with their own assistant.
                    existing = await load_writable_presentation(
                        own_db, target_id, user_id=user_uuid, for_update=True
                    )
                    if existing is None:
                        visible = await load_visible_presentation(
                            own_db, target_id, user_id=user_uuid
                        )
                        if visible is None:
                            return _refusal(
                                f"No presentation `{presentation_id}` you can "
                                "access was found. Omit presentation_id to "
                                "create a new deck."
                            )
                        return _refusal(
                            f"Presentation `{presentation_id}` is shared with "
                            "you read-only, so it cannot be edited in place. "
                            "Copy it into a deck the user owns with "
                            "duplicate_presentation (every slide kept exactly) "
                            "and change the copy with edit_presentation, or ask "
                            "its owner for edit access."
                        )
                    # A revision keeps the deck's look unless the agent changes
                    # it: theme / ratio / transition the call left unset come
                    # from the stored spec, not from the defaults again.
                    stored = existing.spec if isinstance(existing.spec, dict) else {}
                    apply_deck_defaults(
                        spec_payload,
                        {
                            key: str(stored[key])
                            for key in ("theme", "aspect_ratio", "transition")
                            if isinstance(stored.get(key), str) and stored[key]
                        },
                    )
                    if "chrome" not in spec_payload and isinstance(
                        stored.get("chrome"), dict
                    ):
                        spec_payload["chrome"] = stored["chrome"]

                # The template of a NEW deck is the user's choice. In a
                # chat where the user can pick from several templates, the deck
                # is only created once the choice is demonstrably theirs: their
                # request names the template (or asks for none), or the
                # question was already put to them in this conversation and the
                # call relays their answer. Anything else - a blank template_id,
                # or one the model picked on its own after a search_templates -
                # creates nothing and returns the list for the agent to ask
                # with. A single available template needs no question and is
                # used - said so in the result. An automation has nobody to ask
                # and keeps taking the platform default.
                template_note: str | None = None
                template_named = template_id is not None and str(template_id).strip() != ""
                if existing is None and interactive:
                    options = await list_template_options(own_db, user_id=user_uuid)
                    if len(options) > 1:
                        settled = template_named and (
                            template_choice_settled(request_text, str(template_id), options)
                            or await template_choice_asked(conversation_id)
                        )
                        if not settled:
                            await mark_template_choice_asked(conversation_id)
                            return ToolResult(
                                tool_call_id="",
                                content=format_template_choice(
                                    options,
                                    proposed=str(template_id) if template_named else None,
                                ),
                            )
                    elif len(options) == 1 and not template_named:
                        template_id = str(options[0].id)
                        template_note = (
                            "the only template available to this user, so it was used "
                            "without asking"
                        )
                elif existing is None and not template_named:
                    template_note = "platform default: no template was named"

                try:
                    template_uuid, explicit_none = await resolve_deck_template_id(
                        own_db,
                        requested=template_id,
                        existing_template_id=(
                            existing.template_container_id if existing else None
                        ),
                        is_edit=existing is not None,
                    )
                except TemplateChoiceError as exc:
                    return _refusal(str(exc))

                # One load tells everything the link needs to know about the
                # template: that it exists and is a presentation
                # template, its ``deck_defaults`` (a brand deck says
                # ``transition: fade``) that fill whatever is still unset, and
                # the colour schemes it can dress the deck in. Loaded as THIS
                # user may see it: a UUID the model holds is not proof the
                # user may use the template, and an invisible one reads as
                # "not found" - never its name, schemes or defaults.
                facts = await load_visible_template_facts(own_db, template_uuid, user_id=user_uuid)
                if template_uuid is not None and facts is None:
                    if template_id is not None and str(template_id).strip():
                        return _refusal(
                            f"No template `{template_id}` was found. Pick one from "
                            "search_templates, pass 'none' for no template, or omit "
                            "template_id for the platform default."
                        )
                    # The platform default vanished between the lookup and the
                    # load: a free deck is the honest fallback, not a broken link.
                    template_uuid = None
                if facts is not None and not facts.is_presentation_template:
                    return _refusal(
                        f"'{facts.name}' is not a presentation template (kind: "
                        f"{facts.kind or 'unknown'}), so it cannot dress a deck. Pick a "
                        "presentation template from search_templates (kind=presentation)."
                    )
                deck_defaults = dict(facts.deck_defaults) if facts else {}
                if existing is not None:
                    # A revision keeps the covers as the agent re-emits them:
                    # the template's per-cover defaults are for a
                    # deck's birth, and a rewrite that leaves `hero` / `theme`
                    # unset on a title or closing slide means the plain,
                    # inheriting slide it wrote - not the brand cover again on
                    # a deck whose author had chosen otherwise.
                    for key in DECK_SLIDE_DEFAULT_KEYS:
                        deck_defaults.pop(key, None)
                apply_deck_defaults(spec_payload, deck_defaults)
                # The template decides which schemes the deck may be read in:
                # a theme it cannot dress is snapped to its designed scheme and
                # the correction is stated in the result, so the agent never
                # believes it delivered a light deck the brand cannot render.
                theme_note = ""
                if facts is not None and not theme_fits_schemes(
                    spec_payload.get("theme"), facts.supported_schemes
                ):
                    asked = str(spec_payload.get("theme") or "dark")
                    spec_payload["theme"] = fit_theme_to_schemes(asked, facts.supported_schemes)
                    theme_note = (
                        f"Theme: '{asked}' is not supported by the template '{facts.name}' "
                        f"({' / '.join(facts.supported_schemes)}); the deck is "
                        f"'{spec_payload['theme']}'. Link a template that supports "
                        f"'{asked}', or pass template_id 'none' for a free deck."
                    )
                # A slide's OWN theme is held to the same template: a
                # cover asked to be dark under a light-only brand is snapped
                # like the deck would be, and the result names the slide.
                if facts is not None:
                    snapped_slides = fit_slide_themes_to_schemes(spec_payload, facts.supported_schemes)
                    slide_theme_note = slide_theme_snap_note(
                        snapped_slides, spec=spec_payload, template_name=facts.name
                    )
                    if slide_theme_note:
                        theme_note = f"{theme_note}\n{slide_theme_note}" if theme_note else slide_theme_note
                # A template's own cover under any other template, or
                # none, is given that template's default for the cover; the
                # result names the slide.
                heroes, hero_defaults = deck_heroes(facts)
                hero_note = slide_hero_snap_note(
                    fit_slide_heroes(spec_payload, heroes, hero_defaults),
                    spec=spec_payload,
                    template_name=facts.name if facts is not None else None,
                )
                if hero_note:
                    theme_note = f"{theme_note}\n{hero_note}" if theme_note else hero_note

                # The acting user id, for the file-reference check: every file
                # id the slides name must exist and be readable by them
                # — an invented id must fail here, not render as a blank. The
                # user row is loaded by the check itself, and only when the
                # deck names a file.
                # A slide still authored with a retired v1 layout is
                # upgraded by validate_spec inside the save; read what WILL be
                # upgraded off the raw payload first so the agent is told, and
                # stops emitting the retired shape.
                upgrade_note = retired_layout_note(spec_payload)
                # A reporting tree drawn as an architecture is pointed at the
                # org_chart block (a hint: the deck saves as it is).
                hint = org_chart_hint_note(spec_payload)
                upgrade_note = f"{upgrade_note}\n{hint}" if upgrade_note and hint else upgrade_note or hint
                # A rewrite re-emits the pictures the stored deck showed; one
                # whose file is gone is saved as it is, not refused.
                gone: list[tuple[str, str | None, str]] = []
                # The deck's owner stays the owner on a shared edit; the acting
                # user is the AUTHOR of this revision (history + "edited by").
                presentation = await save_presentation(
                    own_db,
                    user_id=existing.user_id if existing is not None else user_uuid,
                    spec_payload=spec_payload,
                    conversation_id=safe_uuid(conversation_id),
                    assignment_run_id=safe_uuid(assignment_run_id),
                    template_container_id=template_uuid,
                    presentation=existing,
                    unlink_template=explicit_none,
                    verify_files_for=user_uuid,
                    verify_carried=slide_file_ids(existing.spec) if existing is not None else (),
                    gone_pictures=gone,
                    author_id=user_uuid,
                    change_summary=(
                        change_summary if isinstance(change_summary, str) else None
                    ),
                    # How long the deck may run is the template's to say:
                    # a free deck takes the platform default.
                    slide_cap=facts.slide_cap if facts is not None else DEFAULT_SLIDE_CAP,
                )
                deck_uuid = presentation.id
                deck_id = str(deck_uuid)
                revision = presentation.revision
                spec = presentation.spec or {}
                linked = facts is not None and presentation.template_container_id is not None
                template_line = template_status_line(
                    facts.name if linked else None,
                    facts.supported_schemes if facts is not None else None,
                    note=template_note if linked else None,
                )
                # What the footer of every slide reads - drawn from the
                # template's pattern when it declares one, from the deck's own
                # event label + footer text otherwise - so the agent tells the
                # user, and knows the footer is ONE deck setting, not slide text.
                footer_line = footer_status_line(spec, facts.chrome if linked else None)
                await own_db.commit()
                # The revision is readable from here. Say so NOW: the
                # layout check below renders the deck in a headless browser
                # before this tool returns, and the stage in the studio, the
                # viewer and the chat card must not wait for that render to
                # show a deck the database already serves. Never raises.
                await emit_tool_event(
                    PRESENTATION_SAVED_EVENT, presentation_id=deck_id, revision=revision
                )
        except PresentationSpecError as exc:
            # Validation failure: the deck was NOT written. The message names the
            # offending field(s), so return it verbatim.
            return _refusal(str(exc))
        except Exception as exc:
            logger.error(
                "generate_presentation failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return ToolResult(
                tool_call_id="",
                content="Error: failed to save the presentation. Please try again.",
                is_error=True,
            )

        # The saved deck is rendered and measured before the agent answers:
        # a slide too dense to fit, a table cut short, a badge running
        # past its pill are reported here, with the slide and the block, so the
        # same turn fixes them. Advisory: the save above already happened.
        layout_note = await layout_check_note(deck_uuid, user_uuid, spec, revision=revision)

        verb = "Updated" if revision > 1 else "Created"
        # Notes coverage rides on every save: the agent learns which
        # content slides it left the presenter nothing to say over, and how
        # long the deck runs read aloud, without a get_presentation round-trip.
        notes = notes_line(spec) if spec_kind(spec) == "deck" else ""
        return ToolResult(
            tool_call_id="",
            content=(
                f"{verb} {_describe(spec)} **{spec.get('title')}** "
                f"({_count(spec)}, theme: {spec.get('theme')}, revision {revision}).\n"
                f"{template_line}\n"
                + (f"{theme_note}\n" if theme_note else "")
                + f"{footer_line}\n"
                + (f"{upgrade_note}\n" if upgrade_note else "")
                + (f"{gone_line}\n" if (gone_line := gone_pictures_line(spec, gone)) else "")
                + f"{summarize_slides(spec)}\n\n"
                + (f"{notes}\n\n" if notes else "")
                + (f"{links}\n\n" if (links := slide_links_line(spec)) else "")
                + (f"{layout_note}\n\n" if layout_note else "")
                + f"presentation_id: `{deck_id}` — change it with edit_presentation "
                "(one slide, block or word at a time; the other slides stay "
                "byte-identical) — what the user sees updates in place.\n"
                f"[[PRESENTATION:{deck_id}]]"
            ),
        )

    async def _edit_presentation(
        presentation_id: str,
        operations: object = None,
        change_summary: str | None = None,
        expected_revision: object = None,
        template_id: object = None,
        **_kw: object,
    ) -> ToolResult:
        """Apply named operations to the STORED spec — the surgical path.

        Nothing here re-generates: the stored spec is the baseline, the
        operations are applied to a copy in code, the patched spec goes through
        the same validation as a fresh deck, and every slide no operation named
        is checked byte-identical to the baseline BEFORE anything is written.
        The result lists exactly which slides changed, so "change one word" is
        provably one word — not a re-emitted deck that reads differently on
        every slide the user did not ask about.

        *template_id* restyles the deck: a UUID links a presentation
        template (palette, fonts, logos on every slide at once), ``'none'``
        frees it. It may travel alone — a template change IS an edit, with no
        slide rewritten — or with operations. The template decides which
        colour schemes the deck may be read in: a theme it cannot dress is
        snapped to the template's own scheme and the snap is stated, while an
        ``update_deck`` that SETS such a theme is refused, never degraded.
        """
        from app.core.database import async_session
        from app.services.presentation_patch import SlideRef
        from app.services.presentations import (
            TEMPLATE_NONE,
            deck_heroes,
            deck_theme_change_note,
            effective_slide_cap,
            fit_slide_heroes,
            fit_slide_themes_to_schemes,
            fit_theme_to_schemes,
            gone_pictures_note,
            introduced_slide_heroes,
            introduced_slide_themes,
            load_visible_presentation,
            load_visible_template_facts,
            load_writable_presentation,
            save_presentation,
            slide_hero_snap_note,
            slide_theme_snap_note,
            theme_fits_schemes,
            unsupported_slide_hero_message,
            unsupported_slide_heroes,
            unsupported_slide_theme_message,
            unsupported_slide_themes,
            unsupported_theme_message,
            verify_file_references,
        )

        _refuse = _refusal

        user_uuid = safe_uuid(user_id)
        if user_uuid is None:
            return _refuse("Presentations are only available to signed-in users.")
        deck_id = safe_uuid((str(presentation_id) if presentation_id else "").strip())
        if deck_id is None:
            return _refuse(
                "presentation_id must be the UUID of an existing deck (from "
                "generate_presentation, get_presentation or a /presentations/ID link)."
            )
        # The template request, resolved before anything is loaded: a UUID
        # links, a none-sentinel unlinks, anything else is refused with what
        # the parameter accepts. ``relink`` is "the agent asked for a template
        # change" - whether it turns out to be one is decided against the row.
        template_text = str(template_id).strip() if template_id not in (None, "") else ""
        relink = bool(template_text)
        unlink = relink and template_text.lower() == TEMPLATE_NONE
        new_template_uuid = safe_uuid(template_text) if relink and not unlink else None
        if relink and not unlink and new_template_uuid is None:
            return _refuse(
                "Nothing was changed: template_id must be the UUID of a presentation "
                "template (from search_templates), 'none' to unlink the current one, or "
                "omitted to keep it."
            )
        has_operations = operations not in (None, "", [])
        if not has_operations and not relink:
            return _refuse(
                "Nothing was changed: pass 'operations' (what to change) and / or "
                "'template_id' (a template to restyle the deck with)."
            )
        ops: list[dict] = []
        if has_operations:
            try:
                ops = coerce_operations(operations)
            except PresentationPatchError as exc:
                return _refuse(f"Nothing was changed: {exc}")
        expected = coerce_revision(expected_revision)
        if expected is None and expected_revision not in (None, ""):
            return _refuse(
                f"Nothing was changed: expected_revision must be the revision number you "
                f"read (a whole number, 1 or more), not {expected_revision!r}."
            )

        try:
            async with async_session() as own_db:
                # FOR UPDATE: two assistants (or two turns of one) editing the
                # same deck serialise on the row, so the second one patches the
                # revision the first one saved instead of a stale baseline -
                # the lost update. The lock spans the
                # patch, the file check and the save, all of it in-process or
                # in this transaction; the layout render happens after commit.
                existing = await load_writable_presentation(
                    own_db, deck_id, user_id=user_uuid, for_update=True
                )
                if existing is None:
                    visible = await load_visible_presentation(own_db, deck_id, user_id=user_uuid)
                    if visible is None:
                        return _refuse(f"No presentation `{presentation_id}` you can access was found.")
                    return _refuse(
                        f"Presentation `{presentation_id}` is shared with you read-only, so "
                        "it cannot be edited. Copy it into a deck the user owns with "
                        "duplicate_presentation (every slide kept exactly) and edit the copy, "
                        "or ask its owner for edit access."
                    )

                # The row lock serialises two editors, but it cannot make a
                # slide NUMBER authored against revision N mean the same slide
                # once revision N+1 inserted or reordered slides. The revision
                # the agent read is the contract: a mismatch is refused before
                # any operation is resolved, and the agent re-reads. The cause
                # is not named as a collaborator: the usual one is the agent's
                # OWN earlier edit in this conversation (every save bumps the
                # revision, and a model re-uses the number it read first), and
                # "someone else saved" would be relayed to a user who is alone
                # on the deck as a fact.
                if expected is not None and existing.revision != expected:
                    return _refuse(
                        f"Nothing was changed: the presentation moved from revision "
                        f"{expected} to {existing.revision} since you read it (a later "
                        "save - an earlier edit of yours, or someone else's). Slide "
                        "numbers may have shifted - read it again with get_presentation "
                        f"and re-apply your operations against revision {existing.revision} "
                        "(or address slides by id)."
                    )

                # The baseline is the stored spec as the validator materialises
                # it (defaults filled, ids present) — the same normalisation the
                # patched spec goes through, so normalisation can never be
                # mistaken for a change and the identity check compares like
                # with like.
                stored = existing.spec if isinstance(existing.spec, dict) else {}
                try:
                    # A read of what is stored, held to the ceiling only:
                    # a deck over its template's cap is still a
                    # deck that can be edited.
                    baseline = validate_spec(stored, slide_cap=MAX_SLIDE_CAP)
                except PresentationSpecError as exc:
                    return _refuse(
                        "The stored presentation no longer passes validation, so it cannot "
                        "be patched in place: read it with get_presentation and rewrite it "
                        f"with generate_presentation(presentation_id=...). Details: {exc}"
                    )

                # The template axis. "Changed" is decided against the
                # row, not the request: re-linking the template the deck
                # already wears is not a change, and unlinking a free deck is
                # not one either.
                current_template = existing.template_container_id
                template_changed = (unlink and current_template is not None) or (
                    new_template_uuid is not None and new_template_uuid != current_template
                )
                effective_template = (
                    None if unlink else (new_template_uuid if relink else current_template)
                )
                # How many slides the deck may hold is the template's to say,
                # so the facts are read BEFORE the operations run when
                # one of them can grow the deck - insert_slide refuses at the
                # cap, naming it. Read as this user may see the template, like
                # every facts load; an invisible one reads as the default. The
                # load is shared with the theme rules below when they need it.
                cap_facts = None
                cap_loaded = False
                slide_cap = DEFAULT_SLIDE_CAP
                if effective_template is not None and any(op.get("op") == "insert_slide" for op in ops):
                    cap_facts = await load_visible_template_facts(
                        own_db, effective_template, user_id=user_uuid
                    )
                    cap_loaded = True
                    if cap_facts is not None:
                        slide_cap = cap_facts.slide_cap
                if ops:
                    try:
                        outcome = apply_operations(baseline, ops, slide_cap=slide_cap)
                    except PresentationPatchNoChangeError as exc:
                        # Nothing to change is not a failure: one line,
                        # not an error, so a wording probe that finds nothing
                        # never counts toward the loop's failure brake - the
                        # loop breaker counts the repeat instead.
                        return ToolResult(
                            tool_call_id="",
                            content=(
                                f"No change: {exc}; nothing was saved (revision "
                                f"{existing.revision} kept). [[PRESENTATION:{deck_id}]]"
                            ),
                            no_op=True,
                        )
                    except PresentationPatchError as exc:
                        return _refuse(f"Nothing was changed: {exc}")
                else:
                    # A template change alone: the spec is the baseline, so
                    # the account below is empty by construction and the
                    # untouched check covers every slide.
                    outcome = PatchResult(spec=copy.deepcopy(dict(baseline)))

                # The facts are loaded whenever the theme must be checked - a
                # template change, or an update_deck that set the theme under
                # the deck's existing template. The slides this edit wrote
                # that name a theme of their own: each is held to the
                # template like the deck theme, so the facts are read whenever
                # one of them is in play.
                written_ids = {ref.id for ref in outcome.changed} | {ref.id for ref in outcome.inserted}
                written_slides = [
                    slide
                    for slide in outcome.spec.get("slides") or []
                    if isinstance(slide, dict) and slide.get("id") in written_ids
                ]
                written_slide_themes = any(slide_theme_override(slide) is not None for slide in written_slides)
                # A written cover naming a template's own hero is held
                # to the template's covers, so the facts are read for it too.
                written_template_heroes = bool(unsupported_slide_heroes({"slides": written_slides}, SHARED_HEROES))
                facts = None
                if effective_template is not None and (
                    template_changed
                    or "theme" in outcome.deck_fields
                    or written_slide_themes
                    or written_template_heroes
                ):
                    facts = (
                        cap_facts
                        if cap_loaded
                        else await load_visible_template_facts(
                            own_db, effective_template, user_id=user_uuid
                        )
                    )
                    if facts is None and template_changed:
                        return _refuse(
                            f"Nothing was changed: no template `{template_text}` was found. "
                            "Pick one from search_templates, or pass 'none' to free the deck."
                        )
                    if facts is not None and template_changed and not facts.is_presentation_template:
                        return _refuse(
                            f"Nothing was changed: '{facts.name}' is not a presentation template "
                            f"(kind: {facts.kind or 'unknown'}), so it cannot dress a deck. Pick a "
                            "presentation template from search_templates (kind=presentation)."
                        )
                theme_note = ""
                if facts is not None and not theme_fits_schemes(
                    outcome.spec.get("theme"), facts.supported_schemes
                ):
                    if "theme" in outcome.deck_fields and not template_changed:
                        # The agent SET a theme this template cannot dress:
                        # refused, never silently degraded - the user asked
                        # for that look and must be told why it is not coming.
                        return _refuse(
                            "Nothing was changed: "
                            + unsupported_theme_message(
                                str(outcome.spec.get("theme")),
                                template_name=facts.name,
                                supported_schemes=facts.supported_schemes,
                            )
                        )
                    # A new template wins over the theme it cannot render (the
                    # brand is what the user asked for); the snap is stated.
                    asked = str(outcome.spec.get("theme") or "dark")
                    snapped = fit_theme_to_schemes(asked, facts.supported_schemes)
                    outcome.spec["theme"] = snapped
                    if "theme" not in outcome.deck_fields:
                        outcome.deck_fields.append("theme")
                    theme_note = (
                        f"Theme snapped from '{asked}' to '{snapped}': the template "
                        f"'{facts.name}' supports {' / '.join(facts.supported_schemes)} only."
                    )

                def _count_snapped(snapped_slides: list[tuple[int, str, str]]) -> None:
                    slides_now = outcome.spec.get("slides") or []
                    for index, _before, _after in snapped_slides:
                        slide = slides_now[index]
                        if slide.get("id") in written_ids or any(ref.id == slide.get("id") for ref in outcome.changed):
                            continue
                        # A slide an operation named but left as it was
                        # DID change now: it moves from the unchanged
                        # account to the changed one, never in both.
                        outcome.named_unchanged = [
                            ref for ref in outcome.named_unchanged if ref.id != slide.get("id")
                        ]
                        outcome.changed.append(
                            SlideRef(
                                index=index + 1,
                                id=str(slide.get("id")),
                                layout=str(slide.get("layout")),
                                title="",
                            )
                        )

                # The slides' OWN themes against the same template.
                # Under the deck's existing template a slide theme this edit
                # INTRODUCES that the template cannot dress is refused, like
                # update_deck is - the user asked for that cover and must be
                # told why it is not coming; an override the stored deck
                # already carried (the template narrowed its schemes since)
                # is not re-refused by a title edit or a move of that slide,
                # the REST PATCH's rule. A NEW template wins over every
                # override it cannot dress: those slides are snapped to its
                # scheme and named, and they count as changed so the identity
                # check below knows they were meant to move.
                if facts is not None:
                    unsupported = unsupported_slide_themes(outcome.spec, facts.supported_schemes)
                    offending = (
                        unsupported
                        if template_changed
                        else introduced_slide_themes(baseline, outcome.spec, unsupported)
                    )
                    if offending and not template_changed:
                        return _refuse(
                            "Nothing was changed: "
                            + unsupported_slide_theme_message(
                                offending,
                                spec=outcome.spec,
                                template_name=facts.name,
                                supported_schemes=facts.supported_schemes,
                            )
                        )
                    if offending:
                        snapped_slides = fit_slide_themes_to_schemes(outcome.spec, facts.supported_schemes)
                        _count_snapped(snapped_slides)
                        note = slide_theme_snap_note(snapped_slides, spec=outcome.spec, template_name=facts.name)
                        theme_note = f"{theme_note}\n{note}" if theme_note else note
                # The covers' heroes, by the same rule: a template's
                # own cover this edit SETS that the deck's template does not
                # offer is refused; a template change gives every cover it does
                # not offer the template's default for it, named and counted.
                if template_changed or written_template_heroes:
                    heroes, hero_defaults = deck_heroes(facts)
                    hero_template = facts.name if facts is not None else None
                    unsupported_heroes = unsupported_slide_heroes(outcome.spec, heroes)
                    offending_heroes = (
                        unsupported_heroes
                        if template_changed
                        else introduced_slide_heroes(baseline, outcome.spec, unsupported_heroes)
                    )
                    if offending_heroes and not template_changed:
                        return _refuse(
                            "Nothing was changed: "
                            + unsupported_slide_hero_message(
                                offending_heroes, spec=outcome.spec, template_name=hero_template, heroes=heroes
                            )
                        )
                    if offending_heroes:
                        snapped_heroes = fit_slide_heroes(outcome.spec, heroes, hero_defaults)
                        _count_snapped(snapped_heroes)
                        note = slide_hero_snap_note(snapped_heroes, spec=outcome.spec, template_name=hero_template)
                        theme_note = f"{theme_note}\n{note}" if theme_note else note

                # Same validation as a fresh deck; a field-addressed error comes
                # back verbatim and nothing is written. The account
                # is then re-derived against the VALIDATED spec: a slide an
                # operation wrote but the validator normalised back (a title
                # with a trailing space, a blank subtitle on a slide that had
                # none) is "already as requested", not "changed" - the result
                # and the history must name only what really differs.
                # The baseline is already upgraded, so a retired v1 shape here
                # is one an OPERATION wrote: noted off the raw patched
                # spec, upgraded by the validation, never stored.
                upgrade_note = retired_layout_note(outcome.spec)
                # Held to the deck's cap, or to its current size when it is
                # already over it: an edit that does not grow the deck
                # is never refused over the cap.
                patched = validate_spec(outcome.spec, slide_cap=effective_slide_cap(slide_cap, baseline))
                outcome = reconcile(outcome, baseline, patched)

                if not template_changed and canonical(patched) == canonical(baseline):
                    # One line: a no-op is a fact, not a report - the
                    # outline it used to carry is what get_presentation is for.
                    # The plain deck marker stays so the card still resolves.
                    return ToolResult(
                        tool_call_id="",
                        content=(
                            f"No change: the presentation already reads that way, so nothing "
                            f"was saved (revision {existing.revision} kept). "
                            f"[[PRESENTATION:{deck_id}]]"
                        ),
                        no_op=True,
                    )

                # The guarantee the tool exists for: every slide no operation
                # named is byte-identical to the baseline. A violation is a bug
                # in the patch engine, so it is logged as one — and refused.
                violations = verify_untouched(baseline, patched, outcome.touched_ids)
                if violations:
                    logger.error(
                        "edit_presentation refused: untouched slides differ after patch (%s): %s",
                        deck_id,
                        "; ".join(violations),
                    )
                    return _refuse(
                        "Refused: the edit would have altered slides no operation named ("
                        + "; ".join(violations)
                        + "). Nothing was saved."
                    )

                # File references are checked on the slides this edit wrote —
                # changed or inserted — against the ACTING user: an
                # invented file id must fail here, not render as a blank. The
                # slides the edit did not touch keep the references they
                # already had; a colleague editing slide 12 of a shared deck
                # is not refused over the owner's own upload on slide 3.
                written = {ref.id for ref in outcome.changed} | {ref.id for ref in outcome.inserted}
                # A reporting tree drawn as an architecture on a slide this
                # edit wrote is pointed at the org_chart block (a hint: the
                # deck saves as it is).
                hint = org_chart_hint_note(patched, slide_ids=written)
                upgrade_note = f"{upgrade_note}\n{hint}" if upgrade_note and hint else upgrade_note or hint
                to_verify = dict(patched)
                to_verify["slides"] = [
                    slide if slide.get("id") in written else {"id": slide.get("id"), "layout": slide.get("layout")}
                    for slide in patched.get("slides") or []
                ]
                # A picture the stored deck already showed whose file is gone (a
                # removed built-in template's asset) does not refuse the
                # edit of its slide: it is saved as it is and named in the result.
                try:
                    gone = await verify_file_references(
                        own_db, to_verify, user=user_uuid, carried=slide_file_ids(baseline)
                    )
                except PresentationSpecError as exc:
                    return _refuse(str(exc))

                # The template change is part of the account: the history line
                # and the result both say what the deck now wears, next to the
                # slide-level operations (which may be none).
                template_summary = ""
                if template_changed:
                    template_summary = (
                        f"Applied template '{facts.name}'"
                        if facts is not None
                        else "Unlinked the template (free deck)"
                    )
                derived = summarize_patch(outcome, patched)
                if template_summary:
                    derived = template_summary if derived == "No change" else f"{template_summary}; {derived}"
                summary = (
                    change_summary.strip()
                    if isinstance(change_summary, str) and change_summary.strip()
                    else derived
                )
                summary = gone_pictures_note(summary, gone)
                previous_revision = existing.revision
                # The deck's owner stays the owner; the acting user is the
                # AUTHOR of this revision (history + "edited by"). The template
                # moves only when the call asked for it; chrome only when an
                # operation changed it.
                presentation = await save_presentation(
                    own_db,
                    user_id=existing.user_id,
                    spec_payload=patched,
                    conversation_id=safe_uuid(conversation_id),
                    assignment_run_id=safe_uuid(assignment_run_id),
                    template_container_id=new_template_uuid if template_changed else None,
                    presentation=existing,
                    unlink_template=unlink and template_changed,
                    author_id=user_uuid,
                    change_summary=summary,
                    slide_cap=slide_cap,
                )
                revision = presentation.revision
                spec = presentation.spec or {}
                template_line = template_status_line(
                    facts.name if facts is not None and presentation.template_container_id else None,
                    facts.supported_schemes if facts is not None else None,
                )
                worn = facts if presentation.template_container_id is not None else None
                if facts is None and presentation.template_container_id is not None:
                    # An untouched template the edit did not need to read: the
                    # line still names it, so the agent knows the deck is not
                    # free - one cheap load, after the row is written, as this
                    # user may see it (an un-shared template reads as free,
                    # exactly what the REST response tells the same user).
                    kept = await load_visible_template_facts(
                        own_db, presentation.template_container_id, user_id=user_uuid
                    )
                    if kept is not None:
                        template_line = template_status_line(kept.name, kept.supported_schemes)
                        worn = kept
                # The footer is read back whenever this edit could have changed
                # what it says: the deck's chrome (event label, footer
                # text, fields, the switch), a template change (a new pattern),
                # or a value the TEMPLATE PATTERN reads - the title, or the
                # {{subtitle}} value. That value is the FIRST title slide's
                # subtitle, so it is compared rendered on both sides rather than
                # through the written slides: deleting the opening slide or
                # moving another title slide ahead of it changes it while
                # writing no slide at all, and a rewrite of the opening slide
                # that leaves its subtitle alone changes nothing the footer
                # shows. Whether the pattern READS the changed value is the same
                # question the layout check asks below, so both decisions go
                # through template_chrome_redraws(): a rename on a deck whose
                # footer never shows the title, or a delete under a pattern that
                # never reads the subtitle, changes nothing the footer says and
                # is not read back - the agent is never sent to look at a footer
                # the edit could not have touched.
                template_chrome = worn.chrome if worn is not None else None
                # Compared as the footer DRAWS it - plain text: a markup-only
                # subtitle edit is not a redraw. If the chrome ever renders
                # markup, this comparison must move to the raw value.
                subtitle_changed = opening_subtitle(baseline) != opening_subtitle(spec)
                chrome_text_changed = template_chrome_redraws(
                    template_chrome, outcome.deck_fields, subtitle_changed=subtitle_changed
                )
                footer_line = (
                    footer_status_line(spec, template_chrome)
                    if template_changed or "chrome" in outcome.deck_fields or chrome_text_changed
                    else ""
                )
                await own_db.commit()
                # Committed and readable: tell the stage before the layout
                # render below — a one-word edit must not be displayed
                # no sooner than a full rewrite. The slide the edit wrote rides
                # along so the studio stage can move to it as the save
                # lands, the way a person would flip to the slide they just
                # asked about. Never raises.
                focus = focus_slide_id(outcome)
                await emit_tool_event(
                    PRESENTATION_SAVED_EVENT,
                    presentation_id=str(deck_id),
                    revision=revision,
                    **({"focus_slide_id": focus} if focus else {}),
                )
                # What the swapped files are, so the account names the
                # new picture and not only its id. Read after the commit: a
                # failed lookup costs the description, never the edit.
                swap_facts: dict[str, PictureFacts] | None = {}
                # Only a file the stored or the saved deck names is described:
                # those are the references the deck already granted
                # this editor or the writer just vouched for. The replacement a
                # chained swap overwrote in the same call was neither.
                shown = {
                    canonical_file_reference(value)
                    for deck in (baseline, spec)
                    for _path, _layout, value in slide_file_references(deck)
                }
                described = {
                    file_id
                    for swap in outcome.image_swaps
                    for file_id in (swap.find, swap.replace)
                    if file_id in shown
                }
                if described:
                    from app.api.v1.helpers import get_user_group_ids
                    from app.services.presentation_pictures import load_picture_facts

                    try:
                        swap_facts = await load_picture_facts(
                            own_db,
                            described,
                            user_id=user_uuid,
                            group_ids=await get_user_group_ids(own_db, user_uuid),
                        )
                    except Exception as exc:
                        logger.warning(
                            "edit_presentation could not describe the swapped pictures: %s",
                            format_exception_message(exc),
                        )
                        swap_facts = None
        except PresentationSpecError as exc:
            return _refuse(str(exc))
        except Exception as exc:
            logger.error(
                "edit_presentation failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return ToolResult(
                tool_call_id="", content="Error: failed to save the presentation. Please try again.", is_error=True
            )

        # The saved revision is rendered and measured like any other save,
        # but the report is scoped to the slides THIS edit wrote: a
        # sentence that no longer fits its slide is the edit's consequence
        # and is fixed in the same turn; a finding on a slide the user did
        # not ask about is counted, not listed, so the edit stays an edit.
        # A change to a deck-level field that is DRAWN on every slide
        # (LAYOUT_DECK_FIELDS: theme, aspect ratio, chrome) re-renders the
        # whole deck, so there the whole report is the edit's consequence.
        # The deck title and the transition are not on a slide, and a move
        # or a delete rewrote no slide's content: with no written slide
        # either, there is nothing new to measure and nothing is rendered.
        # A template change restyles every slide (palette, fonts, logos), so
        # it is measured like a theme change: the whole deck. And a
        # deck whose TEMPLATE footer reads {{title}} (or {{subtitle}}) has its
        # title (or its opening subtitle) on every slide, so for THAT deck a
        # rename - or an edit that changed which subtitle opens the deck, a
        # delete or a move included - redraws the whole deck too: decided per
        # deck (the same template_chrome_redraws() reading that gated
        # the footer read-back above), never by widening the global set.
        redraws_every_slide = (
            template_changed
            or any(name in LAYOUT_DECK_FIELDS for name in outcome.deck_fields)
            or chrome_text_changed
        )
        layout_note = await layout_check_note(
            deck_id,
            user_uuid,
            spec,
            revision=revision,
            written_slide_ids=None if redraws_every_slide else written,
        )

        # A deck theme change says how far it reached: the slides that
        # inherit followed it, the ones with a theme of their own did not.
        deck_theme_line = deck_theme_change_note(spec) if "theme" in outcome.deck_fields else ""
        return ToolResult(
            tool_call_id="",
            content=_render_edit_account(
                spec,
                deck_id=str(deck_id),
                revision=revision,
                previous_revision=previous_revision,
                outcome=outcome,
                summary=summary,
                layout_note=layout_note,
                template_line=template_line,
                template_summary=template_summary,
                theme_note=theme_note,
                deck_theme_line=deck_theme_line,
                footer_line=footer_line,
                upgrade_note=upgrade_note,
                pictures_line="\n".join(
                    line
                    for line in (
                        picture_swaps_line(outcome.image_swaps, swap_facts, described=described),
                        gone_pictures_line(spec, gone),
                    )
                    if line
                ),
            ),
        )

    async def _get_presentation(
        presentation_id: str = "", slides: object = None, reference: object = None, **_kw: object
    ) -> str:
        # The authoring catalogue, served on demand instead of riding in the
        # generate_presentation description on every generation (#8812).
        if isinstance(reference, str) and reference.strip() and not (presentation_id or "").strip():
            return build_presentation_reference(reference)
        from app.core.database import async_session
        from app.services.presentation_comments import (
            format_open_comments_for_agent,
            list_comments,
        )
        from app.services.presentation_public_links import load_public_link, public_link_status_line
        from app.services.presentation_sharing import describe_sharing
        from app.services.presentation_versions import resolve_user_names
        from app.services.presentations import (
            deck_heroes,
            deck_permission,
            load_visible_presentation,
            load_visible_template_facts,
            served_hero_spec,
        )

        deck_id = safe_uuid((presentation_id or "").strip())
        user_uuid = safe_uuid(user_id)
        if deck_id is None:
            return "Error: presentation_id must be a valid UUID."
        if user_uuid is None:
            return "Error: presentations are only available to signed-in users."

        try:
            async with async_session() as own_db:
                loaded = await load_visible_presentation(
                    own_db, deck_id, user_id=user_uuid
                )
                if loaded is None:
                    return (
                        f"No presentation `{presentation_id}` you can access was found."
                    )
                presentation, group_ids = loaded
                spec = presentation.spec or {}
                revision = presentation.revision
                editable = deck_permission(
                    presentation, user_id=user_uuid, user_group_ids=group_ids
                ) in ("owner", "config")
                # Open review notes ride along: a reviewer's "shorten
                # slide 5" is exactly what the owner's assistant should act on
                # next. Each carries its comment id — the handle
                # resolve_presentation_comment takes once the user asks for
                # the note to be closed; this read never closes one itself.
                comments = await list_comments(own_db, presentation.id)
                names = await resolve_user_names(
                    own_db, [c.author_id for c in comments]
                )
                open_comments = format_open_comments_for_agent(
                    comments, spec=spec, names=names
                )
                # The template that brands the deck and the schemes it allows:
                # read AFTER the comments so the fixture order of the
                # existing read-back tests is kept; as this reader may see it,
                # so a template that vanished or was un-shared reads as free -
                # the same answer the REST response gives the same reader.
                facts = await load_visible_template_facts(
                    own_db, presentation.template_container_id, user_id=user_uuid
                )
                template_line = template_status_line(
                    facts.name if facts is not None else None,
                    facts.supported_schemes if facts is not None else None,
                )
                # The covers as the reader sees them: one naming a
                # cover the template does not offer reads as its default.
                spec = served_hero_spec(spec, *deck_heroes(facts))
                # And what the footer reads, so "make it Confidential"
                # is answered with one chrome.fields value, not a slide pass.
                footer_line = footer_status_line(spec, facts.chrome if facts is not None else None)
                # What each picture is: read last, and only for a deck
                # that places one, so a picture-free read costs no query. A
                # description, never a reason for the read itself to fail.
                picture_facts: dict[str, PictureFacts] | None
                try:
                    picture_facts = await load_deck_picture_facts(own_db, spec, user_id=user_uuid, group_ids=group_ids)
                except Exception as exc:
                    logger.warning(
                        "get_presentation could not describe the pictures of %s: %s",
                        deck_id,
                        format_exception_message(exc),
                    )
                    picture_facts = None
                # Who can open it, so "who has access?" is answered from
                # the grants rather than guessed. Read last, and only for a deck
                # whose grants name someone; never a reason for the read to fail.
                try:
                    shared_line = await describe_sharing(own_db, presentation)
                    # And whether a public link opens it for anyone,
                    # and offers its PDF.
                    public_link = await load_public_link(own_db, presentation.id)
                    public_line = public_link_status_line(
                        has_link=public_link is not None,
                        allow_pdf_download=bool(public_link is not None and public_link.allow_pdf_download),
                    )
                    if public_line:
                        shared_line = f"{shared_line}\n{public_line}" if shared_line else public_line
                except Exception as exc:
                    logger.warning(
                        "get_presentation could not name who can open %s: %s",
                        deck_id,
                        format_exception_message(exc),
                    )
                    shared_line = ""
        except Exception as exc:
            logger.error(
                "get_presentation failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return "Error: failed to read the presentation. Please try again."

        all_slides = spec.get("slides") or []
        try:
            selected = parse_slide_selection(slides, all_slides)
        except PresentationPatchError as exc:
            return f"Error: {exc}"

        def _pictures(served: list[int], scope: str) -> str:
            if picture_facts is None:
                return ""
            listed = format_picture_list(
                picture_placements(spec, served),
                picture_facts,
                spec=spec,
                scope=scope,
                editable=editable,
                template_id=str(presentation.template_container_id) if facts is not None else None,
            )
            return f"\n\n{listed}" if listed else ""

        # Someone who can edit the deck is told what to fix; a read-only
        # reader could not act on it, so their read stays cheap. Pinned to the
        # revision just read: right after a save this is the report the save
        # produced, served from its cache rather than rendered a second time.
        layout_note = (
            await layout_check_note(deck_id, user_uuid, spec, revision=revision) if editable else ""
        )

        access = (
            "you can edit it in place: edit_presentation changes one slide, block or "
            "word and leaves every other slide byte-identical; "
            "generate_presentation(presentation_id=...) only to rewrite it wholesale"
            if editable
            else (
                "shared with you READ-ONLY: to change it, copy it with "
                "duplicate_presentation and edit the copy"
            )
        )
        # The marker is the same one generate_presentation returns: a user who
        # opens a deck shared with them ("load /presentations/<id> and show it")
        # gets the deck drawn inline, not a JSON dump. The card keys on the id,
        # so a later in-place edit refreshes this same card.
        notes = notes_line(spec) if spec_kind(spec) == "deck" else ""
        head = (
            f"{_describe(spec).capitalize()} **{spec.get('title')}** (revision {revision}, "
            f"{_count(spec)}; {access}):\n"
            f"{template_line}\n"
            f"{footer_line}\n"
            + (f"{shared_line}\n" if shared_line else "")
            + f"{summarize_slides(spec, with_ids=True)}\n\n"
            + (f"{notes}\n\n" if notes else "")
            + (f"{links}\n\n" if (links := slide_links_line(spec)) else "")
            + (f"{open_comments}\n\n" if open_comments else "")
            + (f"{layout_note}\n\n" if layout_note else "")
            + "To show it in the chat, put this marker on its own line in "
            f"your reply exactly as is:\n[[PRESENTATION:{deck_id}]]\n"
            # The address of one slide, so "where is the pricing
            # slide?" is answered with a link that takes the user there.
            f"To point the user at one {unit_noun(spec, 1)}, link it: "
            f"{slide_link(str(deck_id), 'SLIDE_ID')}\n"
            # The in-deck twin: a link ON a slide, clicked by the audience.
            f"To link one {unit_noun(spec, 1)} to another inside the deck, write "
            "[label](#/SLIDE_ID) in its text.\n\n"
        )

        # Never cut mid-JSON. A spec within budget is returned whole; a
        # selection, or a spec over budget, is served whole slide by whole slide
        # with the deck settings alongside, and the tool says exactly which
        # slides are left and how to read them — so slide 39 of a long deck is
        # as readable as slide 1, and the agent never has to "rebuild" a slide
        # it could not see.
        noun = unit_noun(spec, len(all_slides))
        if selected:
            shown, left = _slide_payloads(spec, selected, _SPEC_READBACK_BUDGET)
            body = (
                f"Deck settings: {_deck_settings(spec)}\n\n"
                f"{noun.capitalize()} {_format_positions(selected[: len(shown)])} in full:\n"
                + "\n".join(shown)
            )
            if left:
                body += (
                    f"\n\nNot shown (over the per-call budget): {noun} {_format_positions(left)}. "
                    f'Call get_presentation again with slides="{_format_positions(left)}".'
                )
            return head + body + _pictures(selected[: len(shown)], f"the {noun} shown")

        payload = json.dumps(spec, ensure_ascii=False, separators=(",", ":"))
        if len(payload) <= _SPEC_READBACK_BUDGET:
            every_slide = list(range(len(all_slides)))
            return head + f"Current spec:\n{payload}" + _pictures(every_slide, f"this {_describe(spec)}")
        shown, left = _slide_payloads(spec, list(range(len(all_slides))), _SPEC_READBACK_BUDGET)
        body = (
            f"The full spec is {len(payload)} chars, over the {_SPEC_READBACK_BUDGET}-char "
            f"per-call budget, so it is served whole {unit_noun(spec, 1)} by whole "
            f"{unit_noun(spec, 1)} (never cut mid-{unit_noun(spec, 1)}).\n"
            f"Deck settings: {_deck_settings(spec)}\n\n"
            f"{noun.capitalize()} {_format_positions(list(range(len(shown))))} in full:\n"
            + "\n".join(shown)
        )
        if left:
            body += (
                f"\n\nNot shown: {noun} {_format_positions(left)}. Read them with "
                f'get_presentation(presentation_id, slides="{_format_positions(left)}") - any '
                'subset works ("20", "16-25", a slide id). To change one you do not need to '
                "read the others: edit_presentation edits one slide, block or word in place."
            )
        return head + body + _pictures(list(range(len(shown))), f"the {noun} shown")

    async def _duplicate_presentation(
        presentation_id: str, title: object = None, **_kw: object
    ) -> ToolResult:
        """Copy a deck the user can READ into a new deck they own, under a new title.

        The library's ``Duplicate`` (``POST /presentations/{id}/duplicate``)
        through the same service, :func:`duplicate_presentation`: the copy
        carries the stored spec as it is - slides, notes, slide ids, theme,
        aspect ratio, chrome - and the template link, and nothing of the
        original's sharing or comments. Reading is the bar, as in the UI: a
        recurring deck is often one a colleague published or shared
        read-only, and copying it gives the user nothing they could not
        already read.
        """
        from app.core.database import async_session
        from app.services.audit import log_audit
        from app.services.presentations import (
            duplicate_presentation,
            load_visible_presentation,
            load_visible_template_facts,
        )

        _refuse = _refusal

        user_uuid = safe_uuid(user_id)
        if user_uuid is None:
            return _refuse("Presentations are only available to signed-in users.")
        source_id = safe_uuid((str(presentation_id) if presentation_id else "").strip())
        if source_id is None:
            return _refuse(
                "presentation_id must be the UUID of the deck to copy (from get_presentation, "
                "an earlier result or a /presentations/ID link)."
            )
        new_title = title.strip() if isinstance(title, str) else ""
        if not new_title:
            return _refuse(
                "Nothing was copied: title is required - the copy's own title, e.g. "
                "'Quarterly Review, Q4 2026'."
            )
        if len(new_title) > MAX_TITLE_CHARS:
            return _refuse(
                f"Nothing was copied: the title is {len(new_title)} characters long, "
                f"the limit is {MAX_TITLE_CHARS}."
            )

        try:
            async with async_session() as own_db:
                loaded = await load_visible_presentation(own_db, source_id, user_id=user_uuid)
                if loaded is None:
                    return _refuse(f"No presentation `{presentation_id}` you can access was found.")
                source, _group_ids = loaded
                source_title = source.title or ""
                try:
                    duplicate = await duplicate_presentation(
                        own_db,
                        source,
                        user_id=user_uuid,
                        title=new_title,
                        conversation_id=safe_uuid(conversation_id),
                        assignment_run_id=safe_uuid(assignment_run_id),
                    )
                except PresentationSpecError as exc:
                    return _refuse(
                        "Nothing was copied: the stored presentation no longer passes validation. "
                        "Read it with get_presentation and build the new deck with "
                        f"generate_presentation instead. Details: {exc}"
                    )
                deck_uuid = duplicate.id
                revision = duplicate.revision
                spec = duplicate.spec or {}
                # As the new owner may see the template, like every read-back:
                # a template they cannot open reads as a free deck.
                facts = await load_visible_template_facts(
                    own_db, duplicate.template_container_id, user_id=user_uuid
                )
                template_chrome = facts.chrome if facts is not None else None
                template_line = template_status_line(
                    facts.name if facts is not None else None,
                    facts.supported_schemes if facts is not None else None,
                )
                footer_line = footer_status_line(spec, template_chrome)
                actor = await _actor(own_db, user_uuid)
                if actor is not None:
                    await log_audit(
                        own_db,
                        user=actor,
                        action="create",
                        entity_type="presentation",
                        entity_id=deck_uuid,
                        entity_name=duplicate.title,
                        summary=f"Duplicated presentation '{source_title}' through an agent",
                        details={
                            "via": "agent",
                            "source_id": str(source.id),
                            "slide_count": duplicate.slide_count,
                        },
                    )
                await own_db.commit()
                await emit_tool_event(
                    PRESENTATION_SAVED_EVENT, presentation_id=str(deck_uuid), revision=revision
                )
        except Exception as exc:
            logger.error(
                "duplicate_presentation failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return ToolResult(
                tool_call_id="", content="Error: failed to copy the presentation. Please try again.", is_error=True
            )

        # The slides are the original's, byte for byte. The one thing the copy
        # draws differently is its title, and only on a deck whose template
        # footer reads {{title}}: there the title is on every slide.
        retitled = new_title != source_title
        layout_note = (
            await layout_check_note(deck_uuid, user_uuid, spec, revision=revision)
            if retitled and template_chrome_redraws(template_chrome, ["title"])
            else ""
        )
        notes = notes_line(spec) if spec_kind(spec) == "deck" else ""
        noun = unit_noun(spec, 1)
        return ToolResult(
            tool_call_id="",
            content=(
                f"Duplicated {_describe(spec)} **{source_title}** into a new {_describe(spec)} "
                f"**{spec.get('title')}** ({_count(spec)}, theme: {spec.get('theme')}, revision "
                f"{revision}). The copy is the user's own: shared with nobody, no review comments; "
                "the original is unchanged.\n"
                f"{template_line}\n"
                f"{footer_line}\n"
                f"{summarize_slides(spec, with_ids=True)}\n\n"
                + (f"{notes}\n\n" if notes else "")
                + (f"{layout_note}\n\n" if layout_note else "")
                + f"presentation_id: `{deck_uuid}` - the copy (the original `{source_id}` stays as "
                f"it was). Make every change on the copy with edit_presentation: one {noun}, block "
                f"or word at a time, by the {noun} ids above (the same as the original's); the "
                f"other {unit_noun(spec, 2)} stay byte-identical.\n"
                f"[[PRESENTATION:{deck_uuid}]]"
            ),
        )

    async def _search_presentation_text(
        presentation_id: str,
        query: object = None,
        case_sensitive: object = None,
        whole_word: object = None,
        **_kw: object,
    ) -> ToolResult:
        """Find every occurrence of a word or phrase in a deck the user can READ, changing nothing.

        Covers exactly the fields ``replace_text`` would touch (one walk,
        ``presentation_patch.search_text``), slide by slide whatever the
        deck's size: ``get_presentation``'s per-call budget is a read-back
        limit, not a search limit, so slide 90 of a long deck is searched
        like slide 1.
        """
        from app.core.database import async_session
        from app.services.presentations import load_visible_presentation

        user_uuid = safe_uuid(user_id)
        if user_uuid is None:
            return _refusal("Presentations are only available to signed-in users.")
        deck_id = safe_uuid((str(presentation_id) if presentation_id else "").strip())
        if deck_id is None:
            return _refusal(
                "presentation_id must be the UUID of the deck to search (from get_presentation, "
                "an earlier result or a /presentations/ID link)."
            )
        if not isinstance(query, str) or not query.strip():
            return _refusal("query is required: the word or phrase to look for, e.g. 'Q3 2026'.")
        if len(query) > MAX_SEARCH_QUERY_CHARS:
            return _refusal(
                f"The query is {len(query)} characters long, the limit is {MAX_SEARCH_QUERY_CHARS}: "
                "search for a shorter, distinctive part of the text."
            )
        sensitive = _flag(case_sensitive)
        whole = _flag(whole_word)

        try:
            async with async_session() as own_db:
                loaded = await load_visible_presentation(own_db, deck_id, user_id=user_uuid)
                if loaded is None:
                    return _refusal(f"No presentation `{presentation_id}` you can access was found.")
                presentation, _group_ids = loaded
                spec = presentation.spec if isinstance(presentation.spec, dict) else {}
                revision = presentation.revision
        except Exception as exc:
            logger.error("search_presentation_text failed: %s", format_exception_message(exc), exc_info=True)
            return ToolResult(
                tool_call_id="", content="Error: failed to read the presentation. Please try again.", is_error=True
            )

        matches = search_text(spec, query, case_sensitive=sensitive, whole_word=whole)
        return ToolResult(
            tool_call_id="",
            content=format_text_search(
                spec, matches, query, revision=revision, case_sensitive=sensitive, whole_word=whole
            ),
        )

    async def _delete_presentation(presentation_id: str, **_kw: object) -> ToolResult:
        """Delete a deck the user OWNS, for good: the library's ``Delete``.

        Through the service behind ``DELETE /presentations/{id}``
        (:func:`remove_presentation`), so what goes with the deck - its
        history, review comments, public link and grants - is the same on
        both surfaces. The bar is ownership, read with the agent-side admin
        rule (:func:`agent_deck_permission`): the owner, or an administrator
        on a company-managed deck. A ``config`` grantee may edit a deck but
        not make it disappear for its owner, and an administrator's agent
        never removes a colleague's personal deck from a chat (the same rule
        as every agent write, ``load_writable_presentation``). There is no
        trash: the description says so, and that it runs only on the user's
        explicit request.
        """
        from app.core.database import async_session
        from app.services.presentations import (
            agent_deck_permission,
            load_visible_presentation,
            remove_presentation,
        )

        user_uuid = safe_uuid(user_id)
        if user_uuid is None:
            return _refusal("Presentations are only available to signed-in users.")
        deck_id = safe_uuid((str(presentation_id) if presentation_id else "").strip())
        if deck_id is None:
            return _refusal(
                "Nothing was deleted: presentation_id must be the UUID of the deck to delete (from "
                "get_presentation, an earlier result or a /presentations/ID link)."
            )

        try:
            async with async_session() as own_db:
                # FOR UPDATE: a delete waits for an edit of the same deck in
                # flight, and an edit queued behind it finds no row.
                loaded = await load_visible_presentation(own_db, deck_id, user_id=user_uuid, for_update=True)
                if loaded is None:
                    return _refusal(
                        f"Nothing was deleted: no presentation `{presentation_id}` you can access was found."
                    )
                presentation, group_ids = loaded
                tier = await agent_deck_permission(own_db, presentation, user_id=user_uuid, user_group_ids=group_ids)
                if tier != "owner":
                    return _refusal(_delete_refusal(presentation, tier))
                spec = presentation.spec if isinstance(presentation.spec, dict) else {}
                revision = presentation.revision
                grants = len(presentation.shared_user_ids or []) + len(presentation.shared_group_ids or [])
                actor = await _actor(own_db, user_uuid)
                await remove_presentation(own_db, presentation, actor=actor, via_agent=True)
                await own_db.commit()
        except Exception as exc:
            logger.error("delete_presentation failed: %s", format_exception_message(exc), exc_info=True)
            return ToolResult(
                tool_call_id="",
                content="Error: failed to delete the presentation; nothing was deleted. Please try again.",
                is_error=True,
            )

        access = (
            f", and the {grants} person/group grant(s) it was shared through no longer open it" if grants else ""
        )
        return ToolResult(
            tool_call_id="",
            content=(
                f"Deleted {_describe(spec)} **{spec.get('title')}** (`{deck_id}`, {_count(spec)}, revision "
                f"{revision}) for good: there is no trash to restore it from. Its version history, review "
                f"comments and public link went with it{access}. Its card in a conversation now reads "
                "'no longer available'. Do not show its marker again; tell the user it is deleted."
            ),
        )

    # ── Review comments ──────────────────────────────────────────
    # Three tools, one contract: every handler runs AS THE CALLING USER on the
    # same service the REST routes use (`presentation_comments`), so the agent
    # reads what its user can read, comments where its user may comment, and
    # resolves only what `can_manage_comment` would let that user resolve by
    # hand. The "only when asked" clause on resolving lives in the tool
    # description and in the ids the list carries — not in a system prompt.

    id_errors = {
        "deck": "Error: presentation_id must be a valid UUID.",
        "user": "Error: presentations are only available to signed-in users.",
    }

    async def _actor(own_db: AsyncSession, user_uuid):
        """The calling user's row, for the audit trail (None if it is gone)."""
        from sqlalchemy import select

        from app.models.user import User

        return (await own_db.execute(select(User).where(User.id == user_uuid))).scalar_one_or_none()

    async def _list_presentation_comments(
        presentation_id: str,
        status: str = "open",
        slide_position: int | None = None,
        slide_id: str | None = None,
        **_kw: object,
    ) -> str:
        from app.core.database import async_session
        from app.services.presentation_comments import (
            COMMENT_STATUSES,
            CommentError,
            format_comments_for_agent,
            list_comments,
            resolve_slide_anchor,
        )
        from app.services.presentation_comments import (
            slide_position as position_of,
        )
        from app.services.presentation_versions import resolve_user_names
        from app.services.presentations import load_visible_presentation

        deck_id = safe_uuid((presentation_id or "").strip())
        user_uuid = safe_uuid(user_id)
        if deck_id is None:
            return id_errors["deck"]
        if user_uuid is None:
            return id_errors["user"]
        wanted = (str(status or "open")).strip().lower()
        if wanted not in COMMENT_STATUSES:
            return f"Error: status must be one of {', '.join(COMMENT_STATUSES)}."

        try:
            async with async_session() as own_db:
                loaded = await load_visible_presentation(own_db, deck_id, user_id=user_uuid)
                if loaded is None:
                    return f"No presentation `{presentation_id}` you can access was found."
                presentation, _group_ids = loaded
                spec = presentation.spec or {}
                title = spec.get("title") or presentation.title
                try:
                    # Same two selectors as add_presentation_comment: the stable
                    # id the listings print wins over a position.
                    anchor = resolve_slide_anchor(spec, slide_id=slide_id, slide_position=slide_position)
                except CommentError as exc:
                    return f"Error: {exc}"
                comments = await list_comments(own_db, presentation.id)
                names = await resolve_user_names(
                    own_db,
                    [c.author_id for c in comments] + [c.resolved_by_user_id for c in comments],
                )
                block = format_comments_for_agent(
                    comments,
                    spec=spec,
                    names=names,
                    status=wanted,
                    slide_id=anchor,
                    limit=_COMMENT_LIST_LIMIT,
                )
        except Exception as exc:
            logger.error(
                "list_presentation_comments failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return "Error: failed to read the comments. Please try again."

        if not block:
            scope = "" if wanted == "all" else f"{wanted} "
            # The resolved position, not the argument: an anchor given by id
            # has no position of its own, and the noun follows the kind.
            where = f" on {unit_noun(spec, 1)} {position_of(spec, anchor)}" if anchor else ""
            return f"No {scope}review comments{where} on **{title}**."
        return f"**{title}**:\n{block}"

    async def _add_presentation_comment(
        presentation_id: str,
        body: str,
        slide_position: int | None = None,
        slide_id: str | None = None,
        reply_to: str | None = None,
        **_kw: object,
    ) -> str:
        from app.core.database import async_session
        from app.services.audit import log_audit
        from app.services.presentation_comments import (
            CommentError,
            create_comment,
            resolve_slide_anchor,
            whole_noun,
        )
        from app.services.presentation_comments import (
            slide_position as position_of,
        )
        from app.services.presentations import load_visible_presentation

        deck_id = safe_uuid((presentation_id or "").strip())
        user_uuid = safe_uuid(user_id)
        if deck_id is None:
            return id_errors["deck"]
        if user_uuid is None:
            return id_errors["user"]
        parent_uuid = None
        if (reply_to or "").strip():
            parent_uuid = safe_uuid(reply_to.strip())
            if parent_uuid is None:
                return "Error: reply_to must be the UUID of an existing comment."

        try:
            async with async_session() as own_db:
                loaded = await load_visible_presentation(own_db, deck_id, user_id=user_uuid)
                if loaded is None:
                    return f"No presentation `{presentation_id}` you can access was found."
                presentation, _group_ids = loaded
                spec = presentation.spec or {}
                title = spec.get("title") or presentation.title
                try:
                    # A reply inherits its thread's slide; an anchor given with
                    # it would silently disagree, so it is ignored on purpose.
                    anchor = (
                        None
                        if parent_uuid is not None
                        else resolve_slide_anchor(spec, slide_id=slide_id, slide_position=slide_position)
                    )
                    comment = await create_comment(
                        own_db,
                        presentation,
                        author_id=user_uuid,
                        body=body,
                        slide_id=anchor,
                        parent_id=parent_uuid,
                    )
                except CommentError as exc:
                    return f"Error: {exc}"
                actor = await _actor(own_db, user_uuid)
                if actor is not None:
                    await log_audit(
                        own_db,
                        user=actor,
                        action="create",
                        entity_type="presentation_comment",
                        entity_id=comment.id,
                        entity_name=presentation.title,
                        summary=f"Commented on presentation '{presentation.title}' through an agent",
                        details={
                            "via": "agent",
                            "presentation_id": str(presentation.id),
                            "slide_id": comment.slide_id,
                            "reply": comment.parent_id is not None,
                        },
                    )
                await own_db.commit()
                comment_id = str(comment.id)
                pos = position_of(spec, comment.slide_id)
                unit = unit_noun(spec, 1)
                where = (
                    f"{unit} {pos}"
                    if pos is not None
                    else (f"its thread's {unit}" if parent_uuid is not None else f"the whole {whole_noun(spec)}")
                )
        except Exception as exc:
            logger.error(
                "add_presentation_comment failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return "Error: failed to add the comment. Please try again."

        kind = "Reply" if parent_uuid is not None else "Comment"
        return f"{kind} [{comment_id}] added on **{title}** ({where}), posted as the user in its Comments panel."

    async def _resolve_presentation_comment(
        presentation_id: str,
        comment_id: str,
        resolved: bool = True,
        reply: str | None = None,
        **_kw: object,
    ) -> str:
        from app.core.database import async_session
        from app.services.audit import log_audit
        from app.services.presentation_comments import (
            CommentError,
            can_manage_comment,
            count_open_comments,
            create_comment,
            get_comment,
            set_comment_resolved,
        )
        from app.services.presentations import (
            agent_deck_permission,
            load_visible_presentation,
        )

        async def open_threads_left(db: AsyncSession, deck: Presentation) -> int:
            # The badge's grouped COUNT of open roots (see the recount below).
            return (await count_open_comments(db, [deck.id])).get(str(deck.id), 0)

        deck_id = safe_uuid((presentation_id or "").strip())
        user_uuid = safe_uuid(user_id)
        note_id = safe_uuid((comment_id or "").strip())
        if deck_id is None:
            return id_errors["deck"]
        if user_uuid is None:
            return id_errors["user"]
        if note_id is None:
            return (
                "Error: comment_id must be the UUID shown in [brackets] by "
                "list_presentation_comments or get_presentation."
            )
        # Default is to close; a model that spells the boolean as text still
        # gets what it meant.
        if resolved is None:
            closing = True
        elif isinstance(resolved, str):
            closing = resolved.strip().lower() not in ("false", "0", "no")
        else:
            closing = bool(resolved)

        try:
            async with async_session() as own_db:
                loaded = await load_visible_presentation(own_db, deck_id, user_id=user_uuid)
                if loaded is None:
                    return f"No presentation `{presentation_id}` you can access was found."
                presentation, group_ids = loaded
                spec = presentation.spec or {}
                title = spec.get("title") or presentation.title
                # Locked: a second resolve racing on the same thread waits here
                # and then finds it already resolved (the no-op below).
                comment = await get_comment(own_db, presentation.id, note_id, for_update=True)
                if comment is None:
                    return f"Error: no comment `{comment_id}` on this presentation."
                permission = await agent_deck_permission(
                    own_db, presentation, user_id=user_uuid, user_group_ids=group_ids
                )
                if not can_manage_comment(comment, user_id=user_uuid, permission=permission):
                    return (
                        "Error: only the comment's author or whoever manages the "
                        "presentation can resolve or reopen it — tell the user "
                        "the note stays open, or reply to it instead."
                    )
                if comment.parent_id is None and closing == (comment.resolved_at is not None):
                    # Idempotent: a retry (the first result never reached the
                    # model) or a thread someone closed meanwhile writes
                    # nothing — no new stamp, no second audit row, and not
                    # the reply either, so a retry cannot post it twice.
                    left = await open_threads_left(own_db, presentation)
                    state = "resolved" if closing else "open"
                    return (
                        f"Comment [{note_id}] on **{title}** is already {state}; nothing changed. "
                        f'To add to the thread anyway, call add_presentation_comment with reply_to="{note_id}". '
                        f"Open review comments left: {left}."
                    )
                try:
                    if (reply or "").strip() and comment.parent_id is None:
                        # The note the reviewer reads when the thread closes:
                        # what was done, in the thread, before the resolution.
                        # Not on a reply: the resolve below refuses it with the
                        # service's own words, which a nested-reply error from
                        # here would otherwise mask.
                        await create_comment(
                            own_db,
                            presentation,
                            author_id=user_uuid,
                            body=reply,
                            parent_id=comment.id,
                        )
                    await set_comment_resolved(own_db, comment, resolved=closing, user_id=user_uuid)
                except CommentError as exc:
                    return f"Error: {exc}"
                actor = await _actor(own_db, user_uuid)
                if actor is not None:
                    await log_audit(
                        own_db,
                        user=actor,
                        action="update",
                        entity_type="presentation_comment",
                        entity_id=comment.id,
                        entity_name=presentation.title,
                        summary=(
                            f"{'Resolved' if closing else 'Reopened'} a comment on "
                            f"'{presentation.title}' through an agent"
                        ),
                        details={
                            "via": "agent",
                            "presentation_id": str(presentation.id),
                            "fields": ["resolved"],
                            "replied": bool((reply or "").strip()),
                        },
                    )
                # Counted BEFORE the commit, inside the same transaction
                # (set_comment_resolved / create_comment flush explicitly, so
                # the resolution above is already visible to the COUNT): a
                # count that fails after the commit would turn a change that
                # landed into an error the agent then retries. One SQL COUNT,
                # the same one the library badge uses - never every comment
                # body loaded to count root threads in Python.
                remaining = await open_threads_left(own_db, presentation)
                await own_db.commit()
        except Exception as exc:
            logger.error(
                "resolve_presentation_comment failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return "Error: failed to update the comment. Please try again."

        verb = "Resolved" if closing else "Reopened"
        replied = " with your reply" if (reply or "").strip() else ""
        return f"{verb} comment [{note_id}] on **{title}**{replied}. Open review comments left: {remaining}."

    # ── Sharing ──────────────────────────────────────────────────
    # AS THE CALLING USER, through the Share dialog's own write
    # (`presentation_sharing.apply_sharing`): only a manager of the deck may
    # share it, only with accounts of this platform, and every change is one
    # audit row. The "only when the user asks" clause lives in the description.

    async def _share_presentation(
        presentation_id: str,
        users: object = None,
        groups: object = None,
        permission: str | None = "read_only",
        action: str | None = "add",
        **_kw: object,
    ) -> str:
        from fastapi import HTTPException

        from app.core.database import async_session
        from app.services.audit import log_audit
        from app.services.presentation_comments import MANAGE_TIERS
        from app.services.presentation_sharing import (
            SHARE_PERMISSIONS,
            apply_sharing,
            audience_line,
            format_sharing_result,
            lock_for_sharing,
            normalize_share_action,
            normalize_share_permission,
            plan_sharing,
        )
        from app.services.presentations import (
            agent_deck_permission,
            load_visible_presentation,
        )
        from app.services.share_principals import (
            SHARE_ACTIONS,
            load_principals,
            principal_entries,
            resolve_principals,
        )

        deck_id = safe_uuid((presentation_id or "").strip())
        user_uuid = safe_uuid(user_id)
        if deck_id is None:
            return id_errors["deck"]
        if user_uuid is None:
            return id_errors["user"]
        verb = normalize_share_action(action)
        if verb is None:
            return f"Error: action must be one of {', '.join(SHARE_ACTIONS)}."
        tier = normalize_share_permission(permission)
        if tier is None and verb != "remove":
            return f"Error: permission must be one of {', '.join(SHARE_PERMISSIONS)}."
        user_entries = principal_entries(users)
        group_entries = principal_entries(groups)
        if verb != "replace" and not user_entries and not group_entries:
            return f"Error: name at least one user (email or user id) or group to {verb}."
        if len(user_entries) + len(group_entries) > MAX_PRINCIPALS_PER_CALL:
            return (
                f"Error: one call names at most {MAX_PRINCIPALS_PER_CALL} people and groups - "
                "share with a group instead, or split the list."
            )

        async def standing_refusal(db: AsyncSession, presentation: Presentation, group_ids: list[str]) -> str | None:
            title = (presentation.spec or {}).get("title") or presentation.title
            if presentation.is_company_managed:
                return (
                    f'Error: "{title}" is published to the whole platform (company-managed): '
                    "its audience is managed by admins from the Share dialog, not by this tool. "
                    "Nothing was changed."
                )
            standing = await agent_deck_permission(db, presentation, user_id=user_uuid, user_group_ids=group_ids)
            if standing not in MANAGE_TIERS:
                return (
                    f'Error: you can view "{title}" but not share it - only its owner or someone '
                    "it is shared with as an editor (can edit) can change who has access. "
                    "Nothing was changed: tell the user to ask the owner."
                )
            return None

        try:
            async with async_session() as own_db:
                loaded = await load_visible_presentation(own_db, deck_id, user_id=user_uuid)
                if loaded is None:
                    return f"Error: no presentation `{presentation_id}` you can access was found. Nothing was shared."
                presentation, group_ids = loaded
                refusal = await standing_refusal(own_db, presentation, group_ids)
                if refusal:
                    return refusal
                resolved = await resolve_principals(own_db, user_entries, group_entries)
                refusal = resolved.refusal(granting=verb != "remove")
                if refusal:
                    return f"Error: {refusal}"
                # The deck is re-read locked: a collaborator who changed its
                # sharing meanwhile (or took the caller's own grant away) is
                # what this call plans from.
                await lock_for_sharing(own_db, presentation, resolved.users)
                refusal = await standing_refusal(own_db, presentation, group_ids)
                if refusal:
                    return refusal
                title = (presentation.spec or {}).get("title") or presentation.title
                index = await load_principals(own_db, presentation)
                index.add(resolved.users)
                index.add(resolved.groups)
                plan = plan_sharing(
                    presentation,
                    action=verb,
                    permission=tier or "read_only",
                    users=resolved.users,
                    groups=resolved.groups,
                )
                if not plan.changed:
                    return f"{format_sharing_result(title, plan, index)}\n{audience_line(presentation, index)}"
                actor = await _actor(own_db, user_uuid)
                if actor is None:
                    return id_errors["user"]
                try:
                    await apply_sharing(own_db, actor, presentation, plan.changes)
                except HTTPException as exc:
                    return f"Error: {str(exc.detail).rstrip('.')}. Nothing was changed."
                await log_audit(
                    own_db,
                    user=actor,
                    action="update",
                    entity_type="presentation",
                    entity_id=presentation.id,
                    entity_name=presentation.title,
                    summary=f"Changed who can open presentation '{presentation.title}' through an agent",
                    details={**plan.audit_details(), "sharing_changed": True},
                )
                audience = audience_line(presentation, index)
                await own_db.commit()
        except Exception as exc:
            logger.error(
                "share_presentation failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return "Error: failed to change who can open the presentation. Nothing was changed."

        return (
            f"{format_sharing_result(title, plan, index)}\n{audience}\n"
            f"This applies now. Those with access find it under Presentations, or at "
            f"/presentations/{deck_id}. To show it in the chat: [[PRESENTATION:{deck_id}]]"
        )

    # ── Public link ──────────────────────────────────────────────
    # The link anyone may open without an account, AS THE CALLING USER with
    # the Share dialog's rules (`presentation_public_links`): only a manager
    # of the deck (an administrator on a company-managed one), refused while
    # the admin switch is off (turning a link off never is), one audit row
    # per change.

    async def _set_presentation_public_link(
        presentation_id: str,
        action: str | None = "enable",
        pdf_download: object = None,
        **_kw: object,
    ) -> str:
        from app.core.database import async_session
        from app.services.audit import log_audit
        from app.services.presentation_comments import MANAGE_TIERS
        from app.services.presentation_public_links import (
            OUTCOME_DISABLED,
            OUTCOME_KEPT,
            OUTCOME_PDF_OFF,
            OUTCOME_PDF_ON,
            PUBLIC_LINK_ACTIONS,
            PUBLIC_LINKS_OFF_DETAIL,
            audit_details,
            audit_summary,
            disable_public_link,
            enable_public_link,
            format_public_link_result,
            normalize_download_flag,
            normalize_public_link_action,
            public_links_enabled,
            set_pdf_download,
        )
        from app.services.presentations import (
            agent_deck_permission,
            load_visible_presentation,
        )

        deck_id = safe_uuid((presentation_id or "").strip())
        user_uuid = safe_uuid(user_id)
        if deck_id is None:
            return id_errors["deck"]
        if user_uuid is None:
            return id_errors["user"]
        verb = normalize_public_link_action(action)
        if verb is None:
            return f"Error: action must be one of {', '.join(PUBLIC_LINK_ACTIONS)}."
        try:
            # A link turned off offers nothing: its option goes with it.
            wanted_pdf = None if verb == "disable" else normalize_download_flag(pdf_download, field="pdf_download")
        except ValueError as exc:
            return f"Error: {exc}. Nothing was changed."
        if verb != "disable" and not public_links_enabled():
            return f"Error: {PUBLIC_LINKS_OFF_DETAIL} Nothing was changed: tell the user."

        try:
            async with async_session() as own_db:
                loaded = await load_visible_presentation(own_db, deck_id, user_id=user_uuid)
                if loaded is None:
                    return f"Error: no presentation `{presentation_id}` you can access was found. Nothing was changed."
                presentation, group_ids = loaded
                title = (presentation.spec or {}).get("title") or presentation.title
                # The Share dialog's rule, server-side: on a company-managed deck
                # an administrator counts as its owner (agent_deck_permission),
                # everyone else reads it only.
                standing = await agent_deck_permission(own_db, presentation, user_id=user_uuid, user_group_ids=group_ids)
                if standing not in MANAGE_TIERS and presentation.is_company_managed:
                    return (
                        f'Error: "{title}" is published to the whole platform (company-managed): only an '
                        "administrator can turn its public link on or off, from the Share dialog or "
                        "through their own assistant. Nothing was changed: tell the user to ask an administrator."
                    )
                if standing not in MANAGE_TIERS:
                    return (
                        f'Error: you can view "{title}" but not share it - only its owner or someone it is '
                        "shared with as an editor (can edit) can turn its public link on or off. "
                        "Nothing was changed: tell the user to ask the owner."
                    )
                actor = await _actor(own_db, user_uuid)
                if actor is None:
                    return id_errors["user"]
                token: str | None = None
                allow_pdf = False
                pdf_changed = False
                if verb == "disable":
                    if not await disable_public_link(own_db, presentation.id):
                        return (
                            f"**{title}** has no public link: nothing to turn off. Only the people it is "
                            "shared with on the platform can open it."
                        )
                    outcome = OUTCOME_DISABLED
                else:
                    change = await enable_public_link(
                        own_db, presentation.id, actor_id=user_uuid, regenerate=verb == "regenerate"
                    )
                    outcome, token, allow_pdf = change.outcome, change.token, change.allow_pdf_download
                    if wanted_pdf is not None:
                        pdf_changed = bool(await set_pdf_download(own_db, presentation.id, wanted_pdf))
                        allow_pdf = wanted_pdf
                # One audit row per change: the link, then its PDF option.
                changes = [outcome] if outcome != OUTCOME_KEPT else []
                if pdf_changed:
                    changes.append(OUTCOME_PDF_ON if allow_pdf else OUTCOME_PDF_OFF)
                for change_outcome in changes:
                    await log_audit(
                        own_db,
                        user=actor,
                        action="update",
                        entity_type="presentation",
                        entity_id=presentation.id,
                        entity_name=presentation.title,
                        summary=f"{audit_summary(change_outcome, presentation.title)} through an agent",
                        details=audit_details(change_outcome, via_agent=True),
                    )
                await own_db.commit()
        except Exception as exc:
            logger.error(
                "set_presentation_public_link failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return "Error: failed to change the public link of the presentation. Nothing was changed."

        return format_public_link_result(
            title, outcome, token, allow_pdf_download=allow_pdf, pdf_changed=pdf_changed
        )

    async def _presentation_poll(
        presentation_id: str,
        action: str | None = "results",
        poll_id: str | None = None,
        question_id: str | None = None,
        **_kw: object,
    ) -> str:
        """Read or drive the live polls of a deck (xtm-one #10157)."""
        import json

        from app.core.database import async_session
        from app.services.presentation_comments import MANAGE_TIERS
        from app.services.presentation_polls import (
            CONTROL_ACTIONS,
            MODE_ACTIONS,
            PollNoopError,
            control_poll,
            deck_poll_states,
            spec_with_poll_mode,
        )
        from app.services.presentations import agent_deck_permission, load_visible_presentation, save_presentation

        deck_id = safe_uuid((presentation_id or "").strip())
        user_uuid = safe_uuid(user_id)
        if deck_id is None:
            return id_errors["deck"]
        if user_uuid is None:
            return id_errors["user"]
        verb = (action or "results").strip().lower()
        if verb != "results" and verb not in CONTROL_ACTIONS + MODE_ACTIONS:
            return f"Error: action must be results or one of {', '.join(CONTROL_ACTIONS + MODE_ACTIONS)}."
        async with async_session() as own_db:
            loaded = await load_visible_presentation(own_db, deck_id, user_id=user_uuid)
            if loaded is None:
                return f"Error: no presentation `{presentation_id}` you can access was found."
            presentation, group_ids = loaded
            if verb == "results":
                states = await deck_poll_states(own_db, presentation)
                await own_db.commit()
                if not states:
                    return "This deck has no poll block."
                for state in states:
                    state.pop("qr", None)
                return json.dumps({"polls": states}, ensure_ascii=False)
            standing = await agent_deck_permission(own_db, presentation, user_id=user_uuid, user_group_ids=group_ids)
            if standing not in MANAGE_TIERS:
                return "Error: only the deck's owner or an editor can drive its polls. Nothing was changed."
            if not poll_id:
                return "Error: poll_id is required for this action (the poll block's id). Nothing was changed."
            if verb in MODE_ACTIONS:
                # guided / autonomous (xtm-one #10235): a new revision of the deck, votes kept.
                try:
                    patched = spec_with_poll_mode(dict(presentation.spec or {}), poll_id.strip(), verb)
                except ValueError as exc:
                    return f"Error: {exc} Nothing was changed."
                if patched is None:
                    return f"No-op: poll {poll_id.strip()!r} is already {verb}. Nothing was changed."
                presentation = await save_presentation(
                    own_db,
                    user_id=presentation.user_id,
                    spec_payload=patched,
                    presentation=presentation,
                    author_id=user_uuid,
                    change_summary=f"Poll {poll_id.strip()} set to {verb}",
                )
                states = [s for s in await deck_poll_states(own_db, presentation) if s["poll_id"] == poll_id.strip()]
                await own_db.commit()
                state = states[0] if states else {}
                state.pop("qr", None)
                return json.dumps(state, ensure_ascii=False)
            try:
                state = await control_poll(own_db, presentation, poll_id.strip(), verb, question_id)
            except PollNoopError as exc:
                return f"No-op: {exc} Nothing was changed."
            except ValueError as exc:
                return f"Error: {exc} Nothing was changed."
            await own_db.commit()
        state.pop("qr", None)
        return json.dumps(state, ensure_ascii=False)

    async def _export_presentation(
        presentation_id: str, format: str = "pdf", **_kw: object
    ) -> ToolResult:
        from app.core.database import async_session
        from app.llm.base import FileRef
        from app.services.presentation_export import (
            EXPORT_FORMATS,
            HeadlessBrowserUnavailableError,
            PresentationRenderError,
            export_presentation,
        )
        from app.services.presentations import (
            load_visible_presentation,
            resolve_template_export_stamp,
        )

        from ._shared import _persist_generated_file

        export_format = (format or "pdf").strip().lower()
        if export_format not in EXPORT_FORMATS:
            return ToolResult(
                tool_call_id="",
                content=f"format must be one of: {', '.join(EXPORT_FORMATS)}.",
                is_error=True,
            )
        deck_id = safe_uuid((presentation_id or "").strip())
        user_uuid = safe_uuid(user_id)
        if deck_id is None:
            return ToolResult(
                tool_call_id="",
                content="presentation_id must be a valid UUID.",
                is_error=True,
            )
        if user_uuid is None:
            return ToolResult(
                tool_call_id="",
                content="Presentations are only available to signed-in users.",
                is_error=True,
            )

        try:
            async with async_session() as own_db:
                # Reading is enough to export: a read-only grantee may take a
                # PDF of a deck shared with them.
                loaded = await load_visible_presentation(
                    own_db, deck_id, user_id=user_uuid
                )
                if loaded is None:
                    return ToolResult(
                        tool_call_id="",
                        content=(
                            f"No presentation `{presentation_id}` you can access was found."
                        ),
                        is_error=True,
                    )
                presentation, _group_ids = loaded
                title = presentation.title
                spec = dict(presentation.spec or {})
                # The render dresses the deck in its template as it is now: an
                # edited template (its palette, chrome, heroes, defaults)
                # restyles the deck without a new revision, so the template's
                # stamp is part of the key.
                template = await resolve_template_export_stamp(
                    own_db,
                    presentation.template_container_id,
                    user_id=user_uuid,
                    deck_theme=presentation.theme,
                )
                key = (deck_id, presentation.revision, export_format, template)
            earlier = exported_files.get(key)
            if earlier is not None:
                return export_result(title, _EXPORT_LABELS[export_format], earlier, again=True)
            # The render happens OUTSIDE the session: it takes seconds and must
            # not hold a pooled connection while Chromium paints.
            exported = await export_presentation(
                presentation_id=deck_id,
                user_id=user_uuid,
                title=title,
                spec=spec,
                export_format=export_format,
            )
            # ROI of chat work (#10544, S2b): an export is a use of the deck.
            from app.services.roi_chat_records import record_signal_detached

            await record_signal_detached(kind="presentation", deliverable_id=deck_id, signal="exported")
            file_att = await _persist_generated_file(
                user_id=user_id,
                filename=exported.filename,
                raw_bytes=exported.data,
                content_type=exported.content_type,
                conversation_id=conversation_id,
                assignment_run_id=assignment_run_id,
            )
        except PresentationRenderError as exc:
            return ToolResult(
                tool_call_id="",
                content=f"The deck could not be exported: {exc}",
                is_error=True,
            )
        except HeadlessBrowserUnavailableError as exc:
            # export_presentation -> render_deck already retried what a retry
            # can cure; the message names the cause and whether to retry.
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Error: the presentation could not be exported ({exc}). The "
                    "user can still export from the deck's toolbar or open it at "
                    f"/presentations/{deck_id}."
                ),
                is_error=True,
            )
        except Exception as exc:
            # Not the browser: the deck rendered, then the packaging or the
            # storage of the file failed. Named as such, never blamed on the
            # browser.
            logger.error(
                "export_presentation failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return ToolResult(
                tool_call_id="",
                content=(
                    "Error: the presentation could not be exported "
                    f"({type(exc).__name__}: {format_exception_message(exc)[:200]}). "
                    "The user can still export from the deck's toolbar or open it at "
                    f"/presentations/{deck_id}."
                ),
                is_error=True,
            )

        # The export log: what the file could not carry the way the
        # viewer shows it - a gauge chart written as a picture. The
        # agent passes it on so the user knows before opening the file.
        stored = TurnExport(
            file_ref=FileRef.from_attachment(file_att),
            filename=exported.filename,
            size=len(exported.data),
            notes=tuple(exported.notes),
        )
        exported_files[key] = stored
        return export_result(title, _EXPORT_LABELS[export_format], stored)

    async def _capture_presentation_slides(
        presentation_id: str,
        slides: object = None,
        file_context: FileContext | None = None,
        **_kw: object,
    ) -> ToolResult:
        from app.services.presentation_export import (
            HeadlessBrowserUnavailableError,
            PresentationRenderError,
            render_deck,
        )
        from app.services.presentations import load_visible_presentation

        from ._shared import _own_session, persist_tool_artifact

        deck_id = safe_uuid((presentation_id or "").strip())
        user_uuid = safe_uuid(user_id)
        if deck_id is None:
            return ToolResult(
                tool_call_id="",
                content="presentation_id must be a valid UUID.",
                is_error=True,
            )
        if user_uuid is None:
            return ToolResult(
                tool_call_id="",
                content="Presentations are only available to signed-in users.",
                is_error=True,
            )

        try:
            async with _own_session() as own_db:
                # Read access is enough to LOOK at a deck, as it is to export
                # one: a read-only grantee may capture a deck shared with them.
                loaded = await load_visible_presentation(
                    own_db, deck_id, user_id=user_uuid
                )
                if loaded is None:
                    return ToolResult(
                        tool_call_id="",
                        content=(
                            f"No presentation `{presentation_id}` you can access was found."
                        ),
                        is_error=True,
                    )
                presentation, group_ids = loaded
                title = presentation.title
                spec = dict(presentation.spec or {})
                revision = presentation.revision
        except Exception as exc:
            logger.error(
                "capture_presentation_slides could not load the deck: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return ToolResult(
                tool_call_id="",
                content="Error: the deck could not be loaded.",
                is_error=True,
            )

        deck_slides = spec.get("slides") or []
        aspect_ratio = str(spec.get("aspect_ratio") or "16:9")
        # Same selector grammar as get_presentation / edit_presentation - an
        # agent that learned "1,4,9-11" on one tool must not meet a second
        # dialect here.
        try:
            picked = parse_slide_selection(slides, deck_slides)
        except PresentationPatchError as exc:
            return ToolResult(tool_call_id="", content=str(exc), is_error=True)
        if not picked:
            picked = _default_capture_slides(len(deck_slides))
        if not picked:
            return ToolResult(
                tool_call_id="",
                content="This deck has no slides to capture.",
                is_error=True,
            )
        if len(picked) > MAX_SLIDE_CAPTURES:
            return ToolResult(
                tool_call_id="",
                content=(
                    f"{len(picked)} slides asked for; {MAX_SLIDE_CAPTURES} is the "
                    "cap - each capture is a full-size image in your context. Pick "
                    "the slides that would teach you something new (the opening, "
                    "the densest one, the closing) and call again for others."
                ),
                is_error=True,
            )

        try:
            # Outside the session: Chromium paints for seconds and must not
            # hold a pooled connection while it does. Only the picked
            # slides are screenshotted: a 100-slide deck reviewed three slides
            # at a time must not allocate 100 full-size PNGs per round.
            rendered = await render_deck(
                deck_id,
                user_uuid,
                aspect_ratio=aspect_ratio,
                want_slide_images=True,
                slide_indices=picked,
            )
        except PresentationRenderError as exc:
            return ToolResult(
                tool_call_id="",
                content=f"The deck could not be rendered: {exc}",
                is_error=True,
            )
        except HeadlessBrowserUnavailableError as exc:
            # render_deck already retried what a retry can cure; the message
            # names the cause and whether a later call can work.
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Error: the deck could not be captured ({exc}). Say so rather "
                    "than describing a render you have not seen."
                ),
                is_error=True,
            )
        except Exception as exc:
            logger.error(
                "capture_presentation_slides failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Error: the deck could not be captured ({type(exc).__name__}: "
                    f"{format_exception_message(exc)[:200]}). Say so rather than "
                    "describing a render you have not seen."
                ),
                is_error=True,
            )

        refs = []
        numbers: list[int] = []
        unchanged: list[int] = []
        # The renderer returns one PNG per REQUESTED slide the rendered deck
        # holds, in the order asked for: a deck edited between the load above
        # and the render can be shorter than its spec, and a pick past its end
        # is skipped wherever it sits in the selection.
        rendered_indexes = [index for index in picked if index < rendered.slide_count]
        for index, png in zip(rendered_indexes, rendered.slide_images, strict=False):
            # The position is part of the key: "unchanged" is said of THIS
            # slide, never of another slide that rendered the same pixels.
            key = (deck_id, index, hashlib.sha256(png).hexdigest())
            earlier = captured_slides.get(key)
            if earlier is not None:
                refs.append(_reused_capture(earlier, file_context))
                numbers.append(index + 1)
                unchanged.append(index + 1)
                continue
            try:
                ref = await persist_tool_artifact(
                    user_id=user_id or "",
                    filename=f"slide-{index + 1:02d}-{deck_id.hex[:8]}.png",
                    raw_bytes=png,
                    content_type="image/png",
                    conversation_id=conversation_id,
                    assignment_run_id=assignment_run_id,
                    # Deliberately NOT registered on the conversation's
                    # FileContext: a ref added there is attached to the
                    # assistant message as a download the user sees
                    # (``_ws_generated_ids`` in api/v1/chat.py), and three
                    # review rounds would leave nine file chips nobody
                    # asked for. The PNG still rides THIS tool result as a
                    # vision block, which is the whole point, and the row
                    # keeps the capture as evidence: a check file,
                    # a working file purged after its retention days.
                    source=AGENT_CHECK_FILE_SOURCE,
                )
            except Exception as exc:
                logger.warning(
                    "Persisting the capture of slide %d failed: %s",
                    index + 1,
                    format_exception_message(exc),
                    exc_info=True,
                )
                continue
            refs.append(ref)
            numbers.append(index + 1)
            captured_slides.setdefault(key, ref)
        logger.info(
            "capture_presentation_slides deck=%s: %d slide(s) persisted, %d reused an earlier capture of this turn",
            deck_id,
            len(numbers) - len(unchanged),
            len(unchanged),
        )
        if not refs:
            return ToolResult(
                tool_call_id="",
                content=(
                    "The deck rendered but no capture could be saved, so there is "
                    "nothing to look at. Say so rather than describing a render you "
                    "have not seen."
                ),
                is_error=True,
            )
        listed = ", ".join(str(number) for number in numbers)
        # Which file is which picture on what the agent is about to look at:
        # the ids a replace_image takes, mapped to what it sees.
        pictures = ""
        indexes = [number - 1 for number in numbers]
        try:
            from app.services.presentation_pictures import load_picture_facts

            picture_spec: dict | None = spec
            placements = picture_placements(spec, indexes)
            moved = rendered.revision is not None and rendered.revision != revision
            if moved or placements:
                async with _own_session() as own_db:
                    if moved:
                        # The page renders the CURRENT row: a save that landed
                        # between the load and the render (a parallel edit of
                        # the same response, a collaborator) put other pictures
                        # in the PNGs, so their ids are read off the revision
                        # rendered - or not listed at all.
                        current = await load_visible_presentation(own_db, deck_id, user_id=user_uuid)
                        picture_spec = (
                            dict(current[0].spec or {})
                            if current is not None and current[0].revision == rendered.revision
                            else None
                        )
                        placements = picture_placements(picture_spec, indexes) if picture_spec is not None else []
                    picture_facts = (
                        await load_picture_facts(
                            own_db,
                            {placement.file_id for placement in placements},
                            user_id=user_uuid,
                            group_ids=group_ids,
                        )
                        if placements
                        else {}
                    )
            if picture_spec is None:
                pictures = (
                    f"\n\nThe deck was saved again while it was captured (revision {revision} -> "
                    f"{rendered.revision}), so its pictures are not listed here: get_presentation "
                    "lists them with their file ids."
                )
            elif placements:
                pictures = "\n\n" + format_picture_list(
                    placements, picture_facts, spec=picture_spec, scope="the captured slides", read_hint=False
                )
        except Exception as exc:
            logger.warning(
                "capture_presentation_slides could not describe the pictures: %s",
                format_exception_message(exc),
            )
        return ToolResult(
            tool_call_id="",
            content=(
                f"Captured slide(s) {listed} of **{title}** ({len(deck_slides)} in "
                f"the deck, {aspect_ratio}). The PNGs are attached to this result as "
                "native vision blocks - LOOK at them and judge the render yourself; "
                "never ask the user to describe or verify it."
                f"{_unchanged_capture_line(unchanged, numbers)}{pictures}"
            ),
            files=refs,
        )

    executor = CallableToolExecutor()
    executor.register("generate_presentation", _generate_presentation)
    executor.register("edit_presentation", _edit_presentation)
    executor.register("get_presentation", _get_presentation)
    executor.register("search_presentation_text", _search_presentation_text)
    executor.register("duplicate_presentation", _duplicate_presentation)
    executor.register("delete_presentation", _delete_presentation)
    executor.register("list_presentation_comments", _list_presentation_comments)
    executor.register("add_presentation_comment", _add_presentation_comment)
    executor.register("resolve_presentation_comment", _resolve_presentation_comment)
    executor.register("capture_presentation_slides", _capture_presentation_slides)
    executor.register("share_presentation", _share_presentation)
    executor.register("set_presentation_public_link", _set_presentation_public_link)
    executor.register("presentation_poll", _presentation_poll)
    executor.register("export_presentation", _export_presentation)
    for _name in executor.tool_names:
        handler = executor.handler(_name)
        if handler is not None and "presentation_id" in inspect.signature(handler).parameters:
            executor.wrap_handler(_name, lambda inner: _with_presentation_id_prefix(inner, user_id))

    # The description is DERIVED from the spec models (``spec_reference()``)
    # plus the validated examples, tone semantics and density budgets in
    # ``services/presentation_guidance`` — a new layout, block or field is
    # documented to the agent the moment it is added to the spec, and every
    # example in it is pinned to ``validate_spec`` by the test suite.
    definitions = [
        ToolDefinition(
            name="generate_presentation",
            description=build_generate_presentation_description(),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Deck title, shown on the chat card and in the browser tab.",
                    },
                    "slides": {
                        "type": "array",
                        "description": (
                            "Ordered slides. Each item is an object with a 'layout' "
                            "key plus that layout's own fields (get_presentation "
                            f"reference=LAYOUT reads them; at most {DEFAULT_SLIDE_CAP} slides unless "
                            "the deck's template sets a higher max_slides, up to "
                            f"{MAX_SLIDE_CAP}; unknown fields are rejected with the "
                            "slide index)."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "layout": {
                                    "type": "string",
                                    "enum": list(SLIDE_LAYOUTS),
                                    "description": (
                                        "Which layout renders this slide - one of "
                                        f"{', '.join(SLIDE_LAYOUTS)}. 'content' with "
                                        "blocks covers every other shape."
                                    ),
                                },
                                "id": {
                                    "type": "string",
                                    "description": (
                                        "Server-assigned slide id; keep it when "
                                        "re-submitting an existing slide, omit on new ones."
                                    ),
                                },
                                "title": {"type": "string", "description": "Inline markup."},
                                "subtitle": {"type": "string", "description": "Inline markup."},
                                "notes": {
                                    "type": "string",
                                    "description": (
                                        "Speaker notes: what the presenter SAYS over "
                                        "this slide (speaker view and PowerPoint notes "
                                        "only, never shown to the room). Inline markup "
                                        "and '- ' list lines allowed."
                                    ),
                                },
                                "blocks": {
                                    "type": "array",
                                    "items": {"type": "object"},
                                    "description": (
                                        "content: single-flow body, each item a block "
                                        "object with a 'type' key (get_presentation "
                                        "reference=BLOCK reads its fields). "
                                        "For side-by-side content pass 'columns' (2-3 "
                                        "arrays of leaf blocks) INSTEAD of blocks."
                                    ),
                                },
                                "ratio": {
                                    "type": "string",
                                    "enum": ["equal", "wide_left", "wide_right"],
                                    "description": "content with exactly 2 columns.",
                                },
                                "vertical_align": {
                                    "type": "string",
                                    "enum": ["start", "center", "end", "stretch"],
                                    "description": (
                                        "content: vertical alignment of the columns "
                                        "(distribute wins over it for the run)."
                                    ),
                                },
                                "footer_block": {
                                    "type": "object",
                                    "description": "One leaf block pinned at the bottom.",
                                },
                                "hide_footer": {"type": "boolean"},
                                "items": {
                                    "type": "array",
                                    "items": {"type": "object"},
                                    "description": (
                                        "agenda: AgendaItem objects {number, title, "
                                        "description, icon}; takeaways: NumberedCard "
                                        "objects {number, title, description, icon}. A "
                                        "plain string is accepted as a title-only item."
                                    ),
                                },
                                "current": {
                                    "type": "integer",
                                    "description": "agenda: 1-based item to highlight.",
                                },
                                # No 'columns' entry, on purpose: this flat per-slide
                                # schema has ONE slot per name and 'columns' is an
                                # integer (1-3) on agenda and takeaways slides but an
                                # array of block arrays on content slides. A type
                                # union is no way out - the Gemini schema cleaner keeps
                                # a single 'type' and drops anyOf/oneOf. The slide
                                # object is open (no additionalProperties), so the
                                # value reaches the Pydantic model, which enforces the
                                # rule; the agent reads it in the description's spec
                                # reference ('columns?: 1|2|3 ... 3 needs 6-9 items'),
                                # pinned by test_agenda_catalog_documents_the_grid_options.
                                "style": {
                                    "type": "string",
                                    "enum": ["list", "grid"],
                                    "description": (
                                        "agenda: grid draws N. | TITLE cells in accent caps "
                                        "with the description under (the default at 3 "
                                        "columns), list the numbered chips."
                                    ),
                                },
                                "distribute": {
                                    "type": "boolean",
                                    "description": (
                                        "agenda: spread the rows evenly over the body "
                                        "(default true); false packs them to the top. "
                                        "content: spread the blocks evenly over the body "
                                        "height (default false; free height lands between "
                                        "blocks, each gap up to a readable maximum), wins "
                                        "over vertical_align for the run; equal_rows wins "
                                        "over it on columns."
                                    ),
                                },
                                "equal_rows": {
                                    "type": "boolean",
                                    "description": (
                                        "content with columns: the Nth block of every "
                                        "column shares one height (rows of equal cards; "
                                        "default false)."
                                    ),
                                },
                                "bento": {
                                    "type": "boolean",
                                    "description": (
                                        "content: lay the blocks out as a bento grid filling "
                                        "the body (default false): 3-5 leaf blocks, 1-3 of "
                                        "them image blocks; the first image is the large "
                                        "cell, the others text or KPI cells. Photos fit "
                                        "cover, screenshots and diagrams contain."
                                    ),
                                },
                                "flow": {
                                    "type": "string",
                                    "enum": ["row", "column"],
                                    "description": (
                                        "agenda: fill order of the grid - row (default) "
                                        "reads 1 2 3 / 4 5 6, column runs down the first "
                                        "column first."
                                    ),
                                },
                                "phases": {
                                    "type": "array",
                                    "items": {"type": "object"},
                                    "description": "roadmap: 2-4 Phase objects.",
                                },
                                "tiles": {
                                    "type": "array",
                                    "items": {"type": "object"},
                                    "description": (
                                        "dashboard: 2-8 MetricItem objects {value, label, "
                                        "sublabel, delta, trend, delta_tone, tone, sparkline[]} - the KPI row."
                                    ),
                                },
                                "tile_size": {
                                    "type": "string",
                                    "enum": list(DASHBOARD_TILE_SIZES),
                                    "description": (
                                        "dashboard: type of the tile row (default md). sm = value "
                                        "36px and 16px label / sublabel / delta, for 5-6 tiles whose "
                                        "sublabel or delta would wrap to a second line."
                                    ),
                                },
                                "panels": {
                                    "type": "array",
                                    "items": {"type": "object"},
                                    "description": (
                                        "dashboard: 1-6 {title, span, block} panels on the "
                                        "panel grid, at most two rows; span 1-3 on 3 columns, "
                                        "1-2 on 2; block is a chart, table, metric or "
                                        "metrics_grid block."
                                    ),
                                },
                                "panel_columns": {
                                    "type": "integer",
                                    "description": (
                                        "dashboard: columns of the panel grid, 2 or 3 (default 3). "
                                        "2 = two equal columns - two panels half and half, a chart "
                                        "beside a table - where a span is 1 or 2."
                                    ),
                                },
                                "number": {
                                    "type": "integer",
                                    "description": "section: watermark chapter number.",
                                },
                                "label": {
                                    "type": "string",
                                    "description": "section: small label above the title.",
                                },
                                "icon": {
                                    "type": "string",
                                    "description": "section: Lucide icon name.",
                                },
                                "event_label": {
                                    "type": "string",
                                    "description": "title: conference / date line.",
                                },
                                "speakers": {
                                    "type": "array",
                                    "items": {"type": "object"},
                                    "description": (
                                        "title: who presents, in speaking order - 1 to 4 "
                                        "{name, title, credentials[], photo_file_id}, drawn side by side."
                                    ),
                                },
                                "speaker": {
                                    "type": "object",
                                    "description": (
                                        "closing: {name, title, credentials[]}. On a title slide it "
                                        "is speakers[0] (the lead speaker); use speakers for several."
                                    ),
                                },
                                "icon_strip": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "title: up to 6 Lucide icon names.",
                                },
                                "align": {
                                    "type": "string",
                                    "enum": ["start", "center"],
                                    "description": (
                                        "title, closing: start (left layout) or center "
                                        "(centred); title default start, closing default center."
                                    ),
                                },
                                "footer": {
                                    "type": "string",
                                    "description": "title: footer line.",
                                },
                                "contacts": {
                                    "type": "array",
                                    "items": {"type": "object"},
                                    "description": (
                                        "closing: up to 6 {kind, value, label} contacts."
                                    ),
                                },
                                "tagline": {
                                    "type": "string",
                                    "description": "closing: line under the contacts.",
                                },
                                "background_image_file_id": {
                                    "type": "string",
                                    "description": "Platform file id of a full-slide background image.",
                                },
                                "theme": {
                                    "type": "string",
                                    "enum": list(PRESENTATION_THEMES),
                                    "description": (
                                        "This slide's own colour scheme (default: inherit the "
                                        "deck theme). If set it always wins over the deck theme "
                                        "and a deck theme change leaves it alone; a dark cover on "
                                        "a light deck is theme 'dark' on the title slide. Held to "
                                        "the deck's template like the deck theme."
                                    ),
                                },
                                "hero": {
                                    "type": "string",
                                    "enum": list(HERO_VARIANTS),
                                    "description": (
                                        "title / closing: the cover backdrop, in the slide's "
                                        "scheme (default plain): gradient = brand gradient wash "
                                        "with the template's mark as a faint watermark, split = "
                                        "text left and a gradient / image panel right, spotlight "
                                        "= accent glow behind the headline with two arcs. Only "
                                        "under a template whose get_template lists them as its "
                                        "own covers: mosaic "
                                        "= pixel blocks clustered in the corners the text leaves "
                                        "free, cascade = pixel blocks stepping down from the "
                                        "top-right corner with the logo large top left and the "
                                        "headline low over a rule and the speaker / footer row, "
                                        "horizon = a skyline of pixel blocks along the bottom edge "
                                        "with the logo large top left, pillar = a column of pixel "
                                        "blocks down the right edge."
                                    ),
                                },
                                "hero_tone": {
                                    "type": "string",
                                    "enum": list(HERO_TONES),
                                    "description": (
                                        "title / closing: the colour the hero is drawn in, from the "
                                        "slide's palette. brand (default) = the template's gradient "
                                        "(gradient_from -> gradient_to; its six chart series for a "
                                        "mosaic); every other value is ONE "
                                        "palette colour alone - accent, gradient_from, gradient_to, "
                                        "a tone (info / success / warning / error) or muted."
                                    ),
                                },
                                "hero_accent_word": {
                                    "type": "boolean",
                                    "description": (
                                        "title: draw the title's last line (after the last line "
                                        "break; the last word of a one-line title) in the accent "
                                        "tone, without markup (default false)."
                                    ),
                                },
                                "overlay": {
                                    "type": "string",
                                    "enum": list(OVERLAY_VARIANTS),
                                    "description": (
                                        "Scrim between background_image_file_id and the content, "
                                        "in the slide's scheme: none (default), soft or strong."
                                    ),
                                },
                                "palette": {
                                    "type": "string",
                                    "enum": list(SLIDE_PALETTES),
                                    "description": (
                                        "This slide's own palette inside the template (omit "
                                        "to keep the template's): xtm_one = accent, block "
                                        "gradients and chart series from the XTM One palette "
                                        "(Violet, Light violet, Medium violet, Sky Blue, Aqua), "
                                        "for a slide about XTM One; logo and footer unchanged."
                                    ),
                                },
                            },
                            "required": ["layout"],
                        },
                    },
                    "theme": {
                        "type": "string",
                        "enum": list(PRESENTATION_THEMES),
                        "description": (
                            "Colour scheme of the deck: 'dark' or 'light'. The "
                            "brand (palette, fonts, logos) comes from the template, "
                            "and a template only supports the schemes its palette "
                            "covers (search_templates lists them). Omit to inherit "
                            "the template's default scheme; falls back to 'dark'. "
                            "A slide with its own 'theme' keeps it whatever this says."
                        ),
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "enum": list(PRESENTATION_ASPECT_RATIOS),
                        "description": (
                            "Slide aspect ratio. Omit to inherit the template's "
                            "default; falls back to 16:9."
                        ),
                    },
                    "transition": {
                        "type": "string",
                        "enum": list(PRESENTATION_TRANSITIONS),
                        "description": (
                            "Slide transition. Omit to inherit the template's "
                            "default; falls back to 'slide'."
                        ),
                    },
                    "chrome": {
                        "type": "object",
                        "description": (
                            "Deck-wide furniture drawn on every slide: "
                            "{show_logo, show_footer, show_slide_numbers "
                            "(booleans, default true), logo_position, event_label, "
                            "footer_text, fields}. logo_position places the logo on "
                            "the title and closing slides only: 'auto' (default) "
                            "follows the slide - bottom-left under a left-aligned "
                            "title, on the axis of a centered one; 'start', 'center' "
                            "or 'end' pin it; body slides keep the corner. The footer "
                            "is ONE deck setting, never slide text: a template footer "
                            "pattern (get_template shows it and its keys) is filled "
                            "from fields {key: value}; without one it reads "
                            "event_label and footer_text. Omit to keep the defaults."
                        ),
                    },
                    "template_id": {
                        "type": "string",
                        "description": (
                            "The deck's TEMPLATE - its design: palette, fonts, logos "
                            "and predefined assets, and the themes (dark / light) it "
                            "allows. The UUID of a presentation template from "
                            "search_templates(kind=\"presentation\"), or 'none' for "
                            "the plain built-in look. The template is the USER'S "
                            "choice, never yours: settle it BEFORE writing the "
                            "slides. In a chat where the user can pick from several "
                            "templates, a new deck is created only when the user "
                            "named the template (or asked for none) or answered "
                            "your question about it; otherwise - omitted, or picked "
                            "by you - the tool creates NO deck and returns the list "
                            "for you to ask the user with. A single available "
                            "template is used without a question. In an automation, "
                            "omitting it takes the platform default. On an edit, "
                            "omitting it keeps the deck's template."
                        ),
                    },
                    "presentation_id": {
                        "type": "string",
                        "description": (
                            "Set ONLY to rewrite an existing deck WHOLESALE — one you "
                            "created, or one shared with you for editing: the deck with "
                            "this id is replaced by the slides you pass, every slide "
                            "re-submitted. For any change to SOME of its slides (a word, "
                            "a bullet, one slide, a reorder) use edit_presentation "
                            "instead, which leaves the other slides byte-identical. "
                            "Omit to create a new deck."
                        ),
                    },
                    "change_summary": {
                        "type": "string",
                        "description": (
                            "On an edit: one line saying what you changed, shown "
                            "in the deck's version history (e.g. 'Shortened slide "
                            "5, added a closing quote')."
                        ),
                    },
                },
                "required": ["title", "slides"],
            },
            source="builtin:generate_presentation",
        ),
        ToolDefinition(
            name="edit_presentation",
            description=_EDIT_DESCRIPTION,
            input_schema=_EDIT_INPUT_SCHEMA,
            # The same deck edited again and again in one run is flagged.
            read_back=WriteReadBack(kind="presentation", identity=("presentation_id",)),
            source="builtin:generate_presentation",
        ),
        ToolDefinition(
            name="get_presentation",
            description=(
                "Read the deck CATALOG with reference (no presentation_id): the fields, "
                "bounds and a minimal example of the layouts, blocks or types you name "
                "(\"content,kpi_row,chart\"), or \"all\" / \"layouts\" / \"blocks\" / \"types\" / "
                "\"examples\" - what generate_presentation and edit_presentation accept. "
                "Or read back a deck — one you generated earlier or one shared with "
                "the user — before changing it, or simply to show it. Always returns "
                "the complete outline (slide number, layout, title, id per slide), "
                "whether you may edit it in place, the reviewers' OPEN comments "
                "(slide-anchored, each with its comment id; address them in your "
                "edit and say which you handled — close one with "
                "resolve_presentation_comment only when the user asks), the "
                "PICTURES of the slides it serves (each file id with what the file "
                "is and where it is shown - what replace_image takes), and the "
                "[[PRESENTATION:ID]] marker that draws the deck in the chat. The "
                "spec follows: whole if it fits the per-call budget, otherwise whole "
                "slide by whole slide with the slides left over named — pass 'slides' "
                "to read exactly the ones you need in full (\"12\", \"16-25\", "
                "\"3,7,9-11\", a slide id). It is never cut mid-slide. To change what "
                "you read, use edit_presentation (one slide, block or word; the "
                "others stay byte-identical); generate_presentation with the same "
                "presentation_id only to rewrite the deck wholesale. To find where a "
                "word or phrase appears (or check it no longer does), call "
                "search_presentation_text instead of reading every slide."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "presentation_id": {
                        "type": "string",
                        "description": (
                            "UUID returned by generate_presentation, or taken from a "
                            "/presentations/ID link the user shared."
                        ),
                    },
                    "slides": {
                        "type": "string",
                        "description": (
                            "Which slides to return in full: 1-based numbers, ranges "
                            "and slide ids, comma-separated (\"12\", \"16-25\", "
                            "\"30-\", \"1,4,9-11\", \"all\"). Omit for the whole spec "
                            "(served slide by slide when over budget)."
                        ),
                    },
                    "reference": {
                        "type": "string",
                        "description": (
                            "Instead of presentation_id: the catalog part to read - "
                            "layout, block or type names comma-separated, or all, "
                            "layouts, blocks, types, examples."
                        ),
                    },
                },
            },
            source="builtin:generate_presentation",
        ),
        ToolDefinition(
            name="search_presentation_text",
            description=(
                "Find every occurrence of a word or phrase in a deck the user can read, WITHOUT "
                "changing anything: whether a name, figure or date still appears anywhere, before "
                "or after an edit. Searches every text field replace_text would touch, on every "
                f"slide whatever the deck's length: {_SEARCHED_FIELDS} (the deck title "
                "and header / footer fields included); never ids, layouts, block types, icons, "
                "tones or file ids. Each occurrence comes with its slide number and id, the block "
                "address edit_presentation takes ('3', '2.1', '4.left.1', 'footer', a dashboard "
                "panel '2'; none for a slide field such as the title or the notes), the field "
                "path inside it (list indexes 0-based) and the text around it with the match "
                "between « » exactly as stored. Matching is literal on the stored text (inline "
                "markup such as **bold** included), case-insensitive unless case_sensitive is "
                "true; whole_word stops 'core' from matching inside 'scores'. Use it instead of "
                "probing with edit_presentation: a replace_text whose find equals its replace "
                "changes nothing and finds nothing."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "presentation_id": {
                        "type": "string",
                        "description": (
                            "UUID of the deck to search, from get_presentation, an earlier "
                            "result or a /presentations/ID link."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": "The word or phrase to look for, e.g. 'Q3 2026'.",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Match the letter case exactly (default false).",
                    },
                    "whole_word": {
                        "type": "boolean",
                        "description": (
                            "Match only where the query is not part of a longer word "
                            "(default false): 'core' then never matches 'scores'."
                        ),
                    },
                },
                "required": ["presentation_id", "query"],
            },
            source="builtin:generate_presentation",
        ),
        ToolDefinition(
            name="duplicate_presentation",
            description=(
                "Copy an existing deck into a NEW deck the user owns, under a new title, in one "
                "call. The copy keeps the template, theme, aspect ratio, header / footer settings "
                "and every slide exactly - layouts, text, speaker notes, slide ids; it is shared "
                "with nobody and carries no review comments. Use it to start a recurring deck "
                "(next quarter's update, this month's report) from the previous edition instead "
                "of generating it again, then change what differs with edit_presentation on the "
                "presentation_id it returns. Any deck the user can read can be copied, one shared "
                "read-only or published to the company included. A footer that reads the deck "
                "title shows the new title. A copy the user no longer wants is removed with "
                "delete_presentation, and only once they ask for it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "presentation_id": {
                        "type": "string",
                        "description": (
                            "UUID of the deck to copy, from get_presentation, an earlier "
                            "result or a /presentations/ID link."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": (
                            "Title of the copy, e.g. 'Quarterly Review, Q4 2026'. Only the "
                            "deck title changes: the opening slide's title and subtitle are slide "
                            "text - update them with edit_presentation."
                        ),
                    },
                },
                "required": ["presentation_id", "title"],
            },
            source="builtin:generate_presentation",
        ),
        ToolDefinition(
            name="delete_presentation",
            description=(
                "Delete a deck the user OWNS. IRREVERSIBLE from here: there is no trash and no "
                "restore - the deck, its version history, review comments, sharing and public link "
                "are gone for everyone, and its card in conversations reads 'no longer available'. "
                "Call it ONLY when the user explicitly asks to delete that deck (for example the "
                "copy made with duplicate_presentation, once they say it is no longer needed); "
                "never on your own initiative to tidy up, and never to start over - edit the deck "
                "with edit_presentation or rewrite it with generate_presentation instead. Only the "
                "owner can delete a deck (an administrator also a deck published to the company): "
                "one shared with the user, even for editing, is refused and left as it is."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "presentation_id": {
                        "type": "string",
                        "description": (
                            "UUID of the deck to delete, from get_presentation, an earlier "
                            "result or a /presentations/ID link."
                        ),
                    },
                },
                "required": ["presentation_id"],
            },
            source="builtin:generate_presentation",
        ),
        ToolDefinition(
            name="list_presentation_comments",
            description=(
                "List the review comments on a presentation you or the user can "
                "read: open ones by default, or the resolved / all threads, "
                "optionally for one slide (page, for a portrait presentation). "
                "Each thread shows its comment id in [brackets], the slide "
                "(1-based position, title and stable id), the author, the text "
                "and its most recent replies. Use it to see what reviewers asked "
                "for before an edit, to check what is left after one, or to find "
                "the id resolve_presentation_comment and add_presentation_comment"
                "(reply_to=...) take."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "presentation_id": {
                        "type": "string",
                        "description": "UUID of the presentation.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["open", "resolved", "all"],
                        "description": "Which threads to list (default open).",
                    },
                    "slide_position": {
                        "type": "integer",
                        "description": (
                            "Only the comments anchored to this slide, 1-based as in "
                            "the outline (omit slide_position AND slide_id for every thread)."
                        ),
                    },
                    "slide_id": {
                        "type": "string",
                        "description": (
                            "Alternative to slide_position: the slide's stable id as "
                            "shown in [id: ...] by the listings (wins when both are given)."
                        ),
                    },
                },
                "required": ["presentation_id"],
            },
            source="builtin:generate_presentation",
        ),
        ToolDefinition(
            name="add_presentation_comment",
            description=(
                "Leave a review note on a presentation, posted in its Comments "
                "panel as the user you act for — on one slide or page "
                "(slide_position, 1-based as in the outline, or the slide's "
                "stable id), on the whole presentation (no anchor), or as a reply "
                "in an existing thread (reply_to = the comment id). Use it when "
                "the user asks you to comment, to leave feedback for a "
                "collaborator, or to answer a reviewer's note — a plain-text note, "
                "at most 4000 characters, no markup. Anyone who can read the "
                "presentation may comment; a reply always lands on its thread's "
                "slide."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "presentation_id": {
                        "type": "string",
                        "description": "UUID of the presentation.",
                    },
                    "body": {
                        "type": "string",
                        "description": "The note, as a reviewer would write it (plain text).",
                    },
                    "slide_position": {
                        "type": "integer",
                        "description": (
                            "Slide (or page) the note is about, 1-based as in the outline. "
                            "Omit slide_position AND slide_id for a note on the whole presentation."
                        ),
                    },
                    "slide_id": {
                        "type": "string",
                        "description": (
                            "Alternative to slide_position: the slide's stable id as "
                            "shown in [id: ...] by the comment listings."
                        ),
                    },
                    "reply_to": {
                        "type": "string",
                        "description": (
                            "Comment id of the thread to reply in; the slide is then "
                            "inherited and any anchor given here is ignored."
                        ),
                    },
                },
                "required": ["presentation_id", "body"],
            },
            source="builtin:generate_presentation",
        ),
        ToolDefinition(
            name="resolve_presentation_comment",
            description=(
                "Close (resolved=true, the default) or reopen (resolved=false) a "
                "review thread by its comment id, optionally posting a short "
                "reply first that says what was done. Call it ONLY when the user "
                "asked for the note to be closed — 'implement the comments and "
                "close them', 'mark that one done' — and AFTER the change it "
                "asked for is saved (edit_presentation, or generate_presentation "
                "for a wholesale rewrite); a note you merely "
                "read or addressed without being asked stays open for a person to "
                "confirm. Allowed for the comment's author and for whoever manages "
                "the presentation (the same rule as the Comments panel); otherwise "
                "it refuses and you say so."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "presentation_id": {
                        "type": "string",
                        "description": "UUID of the presentation.",
                    },
                    "comment_id": {
                        "type": "string",
                        "description": (
                            "The thread's comment id, as shown in [brackets] by "
                            "list_presentation_comments / get_presentation (a root "
                            "comment, not a reply)."
                        ),
                    },
                    "resolved": {
                        "type": "boolean",
                        "description": "true to close the thread (default), false to reopen it.",
                    },
                    "reply": {
                        "type": "string",
                        "description": (
                            "Optional reply posted in the thread before it closes: what changed and where (plain text)."
                        ),
                    },
                },
                "required": ["presentation_id", "comment_id"],
            },
            source="builtin:generate_presentation",
        ),
        ToolDefinition(
            name="capture_presentation_slides",
            description=(
                "LOOK at a deck you generated: renders it server-side and attaches "
                "the chosen slides to the result as images you can actually see. "
                "Use it to check your own work before calling a deck done - a "
                "slide that overflows, a palette that reads wrong on the "
                "background, a logo that never loaded, a chart nobody can decode - "
                "and to compare a deck against a design it is meant to match. "
                "Each capture costs real context, so pick the slides that teach "
                f"you something (max {MAX_SLIDE_CAPTURES} per call); leave 'slides' "
                "unset for the opening, the middle and the closing slide; the "
                "result names the file id behind each picture on them. This is "
                "NOT an export: it produces no file for the user (that is "
                "export_presentation) - the pixels are for your eyes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "presentation_id": {
                        "type": "string",
                        "description": "UUID returned by generate_presentation.",
                    },
                    "slides": {
                        "type": "string",
                        "description": (
                            "Which slides, same selector as get_presentation: "
                            "\"3\", \"1,4\", \"2-4\" or a slide id. Omit for the "
                            "opening, middle and closing slide."
                        ),
                    },
                },
                "required": ["presentation_id"],
            },
            source="builtin:generate_presentation",
        ),
        ToolDefinition(
            name="share_presentation",
            description=(
                "Change who can open a presentation: give people (their email or "
                "user id on this platform) and groups access, change their access, "
                "or take it away. Call it ONLY when the user asks to share, unshare "
                "or change access, and name the people and groups who will get or "
                "lose access in your reply before you call it. It acts as the user "
                "you act for, with the Share dialog's rules: only the presentation's "
                "owner or someone it is shared with as an editor may share it, and "
                "only with existing accounts - an address no account matches is "
                "refused by name and nobody is invited. permission: 'read_only' "
                "(can view: open, present, export, duplicate, comment) or 'config' "
                "(can edit, also through their own assistant). action: 'add' "
                "(default; naming someone already shared with changes their access), "
                "'remove' (revokes at once) or 'replace' (the audience becomes exactly "
                "the people and groups listed - everyone else loses access). "
                "Publishing to the whole company is for admins, from the Share "
                "dialog. get_presentation shows who has access. For people "
                "without an account here (a customer, a partner), use "
                "set_presentation_public_link instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "presentation_id": {
                        "type": "string",
                        "description": "UUID of the presentation.",
                    },
                    "users": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "People, by email address or platform user id "
                            f"(at most {MAX_PRINCIPALS_PER_CALL} people and groups per call)."
                        ),
                    },
                    "groups": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Platform groups, by group id or exact group name.",
                    },
                    "permission": {
                        "type": "string",
                        "enum": ["read_only", "config"],
                        "description": "Access to give (default read_only). Ignored by 'remove'.",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove", "replace"],
                        "description": "What to do with the people and groups listed (default add).",
                    },
                },
                "required": ["presentation_id"],
            },
            source="builtin:generate_presentation",
        ),
        ToolDefinition(
            name="set_presentation_public_link",
            description=(
                "Turn on, replace or turn off the PUBLIC link of a presentation: a "
                "unique URL that lets ANYONE who has it view the deck read-only "
                "without signing in (no speaker notes, comments or editing) - for "
                "people outside the platform, a customer, a partner, an audience. "
                "Call it ONLY when the user asks for a public link or to share the "
                "deck with people who have no account here, and say in your reply "
                "that anyone with the link can view it. 'enable' (default) turns the "
                "link on, or returns the existing one unchanged; 'regenerate' "
                "replaces it (the old URL stops working at once); 'disable' turns it "
                "off. It acts as the user you act for: only the deck's owner, an editor "
                "or, on a company-managed deck, an admin may do it; an admin can turn "
                "public links off platform-wide. Give the user the returned URL as is. "
                "pdf_download: true also lets the link's visitors download a PDF of "
                "the slides (no speaker notes), false stops it; omit it to leave it as "
                "it is - a new link offers no download. Set it only when the user asks. "
                "For colleagues with an account, use share_presentation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "presentation_id": {
                        "type": "string",
                        "description": "UUID of the presentation.",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["enable", "regenerate", "disable"],
                        "description": "What to do with the public link (default enable).",
                    },
                    "pdf_download": {
                        "type": "boolean",
                        "description": (
                            "Whether the link's visitors may also download the slides as a PDF. "
                            "Omit to leave it unchanged (a new link starts without it). Ignored by 'disable'."
                        ),
                    },
                },
                "required": ["presentation_id"],
            },
            source="builtin:generate_presentation",
        ),
        ToolDefinition(
            name="presentation_poll",
            description=(
                "Read or drive the live polls (poll blocks) of a presentation. 'results' "
                "(default) returns every poll: its audience link, the open question, "
                "locked / revealed, the voters and, per question, the counts per "
                "option, the rating average or the words. The other actions need poll_id "
                "and the owner or an editor: 'open' (with question_id), 'next', 'lock', "
                "'unlock', 'reveal' and 'hide' (the quiz answer), 'reset' (deletes every "
                "vote; only when the user asks), 'guided' or 'autonomous' (the mode; votes kept)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "presentation_id": {"type": "string", "description": "UUID of the presentation."},
                    "action": {
                        "type": "string",
                        "enum": [
                            "results",
                            "open",
                            "next",
                            "lock",
                            "unlock",
                            "reveal",
                            "hide",
                            "reset",
                            "guided",
                            "autonomous",
                        ],
                        "description": "Default results.",
                    },
                    "poll_id": {"type": "string", "description": "The poll block's id."},
                    "question_id": {"type": "string", "description": "For open: the question's id."},
                },
                "required": ["presentation_id"],
            },
            source="builtin:generate_presentation",
        ),
        ToolDefinition(
            name="export_presentation",
            description=(
                "Export a deck you generated to a downloadable file, rendered "
                "server-side exactly as it looks on screen: 'pdf' (vector, one "
                "page per slide), 'pptx' (PowerPoint with one pixel-perfect image "
                "per slide + speaker notes) or 'pptx_native' (PowerPoint with "
                "editable text boxes and shapes). Only when the user asks for a "
                "file — the deck itself is already viewable and exportable from "
                "its toolbar. Include the returned [[FILE:...]] marker verbatim."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "presentation_id": {
                        "type": "string",
                        "description": "UUID returned by generate_presentation.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["pdf", "pptx", "pptx_native"],
                        "description": "Output format (default pdf).",
                    },
                },
                "required": ["presentation_id"],
            },
            source="builtin:generate_presentation",
        ),
    ]

    return definitions, executor
