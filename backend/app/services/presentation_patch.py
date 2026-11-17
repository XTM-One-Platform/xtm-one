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

"""Surgical edits to a stored ``PresentationSpec``.

Why this module exists
----------------------
The agent's only write path used to be ``generate_presentation``, which takes
the COMPLETE slide list on every call. Asked to change one word on slide 12 of
a 39-slide deck, a model has to re-emit all 39 slides — and a model re-emitting
a slide never produces the same slide: titles get rephrased, bullets reordered,
notes rewritten. The user asked for one word and got a deck that moved under
them. On long decks it was not even possible: ``get_presentation`` cut the spec
at a fixed budget, so the slides past it could not be read, let alone re-sent.

:func:`apply_operations` is the alternative: a short list of named operations,
applied server-side to the STORED spec, each addressing one slide (by 1-based
index or stable id) and optionally one block inside it. The result carries the
patched spec plus an exact account of what was touched, and
:func:`verify_untouched` proves — by canonical JSON comparison — that every
slide no operation named is byte-identical to what was stored. The guarantee
the user needs ("change this word and NOTHING else moves") is therefore
enforced in code, not requested of the model.

Pure functions, no database. The tool in ``builtin_tools/presentation.py``
loads the deck, runs the stored spec through ``validate_spec`` to get its
canonical form (the baseline), applies the operations, validates the result (a
broken replacement is a field-addressed error and nothing is saved), verifies
the untouched slides, and saves through the same ``save_presentation`` path a
full generation uses — same permission tiers, same file verification, same
revision bump and version snapshot.

Addressing
----------
* A **slide** is a string: a slide id first, a digit string as a 1-based
  position (the number in every outline). The tool schema declares the
  string form only - a union type is refused by Gemini's function
  declarations - but a JSON integer is read as a position too, for a provider
  that ignores the declared type.
* A **block** is a dotted path of 1-based positions read from the slide down:
  ``"3"`` is the third block of a ``blocks`` slide; on a ``columns`` slide the
  first segment is the column (``"2.3"`` = column 2, block 3); a further
  segment steps into a block's own nested blocks — a callout's ``children``
  (``"3.2"`` = the callout's second child), a ``columns`` BLOCK's columns and
  then the block inside (``"3.2.1"`` = block 3, column 2, block 1; ``"3.2"``
  alone names the column, which no block operation accepts), a
  ``comparison`` block's ``left`` / ``right`` panel (``"4.left.1"``).
  ``"footer"`` is the slide's ``footer_block``.
* On a ``dashboard`` the body is its panels, each holding ONE figure:
  ``"2"`` is panel 2. ``update_block`` sets its figure's fields and the
  panel's own (``title``, ``span`` - a figure in a panel carries neither),
  ``replace_block`` swaps the figure under the same title, ``insert_block``
  adds a panel holding the block and ``delete_block`` removes the panel - so
  one chart of a six-panel dashboard changes without the other five being
  re-sent.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.utils import is_valid_uuid
from app.services.presentation_chrome import merge_deck_fields
from app.services.presentation_pictures import (
    canonical_file_reference,
    describe_location,
    is_file_reference_key,
    normalize_file_reference,
)
from app.services.presentation_spec import (
    DEFAULT_SLIDE_CAP,
    DashboardPanel,
    clamp_slide_cap,
    slide_cap_hint,
    slide_model,
    unit_noun,
    whole_noun,
)

#: Every operation ``edit_presentation`` accepts, in the order they are documented.
OPERATIONS: tuple[str, ...] = (
    "update_slide",
    "replace_slide",
    "insert_slide",
    "delete_slide",
    "move_slide",
    "update_block",
    "replace_block",
    "insert_block",
    "delete_block",
    "replace_text",
    "replace_image",
    "update_deck",
)

#: One-line contract per operation. The tool description is DERIVED from this
#: table, so an operation cannot exist without being documented to the agent.
OPERATION_CONTRACTS: dict[str, str] = {
    "update_slide": (
        "{slide, fields}: set the given top-level fields of ONE slide (title, "
        "subtitle, notes, blocks, items, theme, hero, hero_tone, overlay, ...); a null value "
        "removes the field - {theme: null} gives a slide back to the deck theme. "
        "layout and id cannot change (use replace_slide)."
    ),
    "replace_slide": (
        "{slide, content}: replace ONE slide with a complete slide object "
        "(layout + fields); it keeps the slide's id, so anchored comments stay."
    ),
    "insert_slide": (
        "{content, at | after}: add a complete slide object at 1-based position "
        "'at' (existing slides shift down) or right after slide 'after'; "
        "omit both to append."
    ),
    "delete_slide": "{slide}: remove ONE slide.",
    "move_slide": "{slide, to}: move ONE slide to 1-based position 'to' in the resulting order.",
    "update_block": (
        "{slide, block, fields}: set the given fields of ONE block; a null value "
        "removes the field. type cannot change (use replace_block). On a dashboard, "
        "block 'N' is panel N: fields set its figure, and title / span set the panel's own."
    ),
    "replace_block": (
        "{slide, block, content}: replace ONE block with a complete block object "
        "(type + fields); a dashboard panel keeps its title and span."
    ),
    "insert_block": (
        "{slide, content, at}: add a complete block object at block address 'at' "
        "(the position it takes; existing blocks shift down; 'footer' sets the "
        "footer_block). Omit 'at' to append to a blocks slide. On a dashboard it "
        "adds a panel holding the block (untitled, span 1)."
    ),
    "delete_block": "{slide, block}: remove ONE block (on a dashboard, panel N and its figure).",
    "replace_text": (
        "{find, replace, slide?, count?}: exact, case-sensitive text replacement "
        "in every text field of ONE slide ('slide') or of the whole deck (no "
        "'slide'); ids, layouts, block types, icons, tones and file ids are never "
        "touched. Refused when 'find' is absent, when 'count' is given and does "
        "not equal the number of occurrences found, and when no 'slide' is given "
        "and the text occurs more than once (the refusal lists where) - so a "
        "phrase that occurs twice is never changed blindly: scope it, or pass "
        "count to change every occurrence on purpose. To only CHECK where a text "
        "occurs, call search_presentation_text (read-only), never a replace_text "
        "whose find equals its replace."
    ),
    "replace_image": (
        "{find, replace, slide?, block?, count?}: swap a PICTURE by file id wherever "
        "it is shown - an image block, one logo of a logo_grid, a profiles photo or "
        "line logo, an architecture node or zone logo, a slide's background image or speaker "
        "photo - without re-sending the block or its list. 'find' is the file id shown "
        "now (get_presentation lists every picture with its file id and place), "
        "'replace' the new one (a template asset id from get_template, or a file "
        "attached to this conversation). Scope it to one slide ('slide') or one block "
        "('slide' + 'block'). A file not shown there changes nothing; like "
        "replace_text it is refused when 'count' differs from the places found, and "
        "when no 'slide' is given and the picture is shown more than once (the "
        "refusal lists the places)."
    ),
    "update_deck": (
        "{fields}: set deck-level fields - title, theme, aspect_ratio, "
        "transition, chrome (chrome and chrome.fields are merged key by key; "
        "null removes a key). A deck theme change reaches only the slides that "
        "inherit it: a slide with its own theme keeps it (the result says which)."
    ),
}
assert tuple(OPERATION_CONTRACTS) == OPERATIONS  # noqa: S101 - import-time contract

#: Deck-level fields ``update_deck`` may set.
DECK_FIELDS: tuple[str, ...] = ("title", "theme", "aspect_ratio", "transition", "chrome")

#: The deck-level fields that are DRAWN on every slide, so a change to one of
#: them redraws the whole deck and the layout check after such an edit is
#: about every slide, not the slides the edit wrote. The other two are not on
#: a slide at all BY THEMSELVES: ``title`` names the deck in the library, the
#: browser tab and the chat card (the v1 chrome footer shows
#: ``chrome.event_label`` and ``chrome.footer_text``, never the title) and
#: ``transition`` is the motion BETWEEN slides, which moves no pixel of a
#: slide at rest - a rename or a "use fade" must not render the deck and hand
#: the agent a deck-wide report. The one exception is decided by the
#: caller, not here: a deck whose TEMPLATE footer pattern reads ``{{title}}``
#: does draw the title on every slide, and ``edit_presentation`` widens
#: the check for that deck only (``template_chrome_redraws``). The single
#: source of truth for that scoping; the docs quote it.
LAYOUT_DECK_FIELDS: frozenset[str] = frozenset({"theme", "aspect_ratio", "chrome"})
assert LAYOUT_DECK_FIELDS.issubset(DECK_FIELDS)  # noqa: S101 - import-time contract

#: Operations one ``edit_presentation`` call may carry. An edit is a handful
#: of pointed changes; a burst past this is a rewrite in disguise and belongs
#: to ``generate_presentation``, whose result the agent then reviews whole.
MAX_OPERATIONS = 50

#: Keys whose string values ``replace_text`` never rewrites: identity, layout
#: and type tags, enum-like choices, icon names. A file reference
#: (``is_file_reference_key``) is skipped as well - ``replace_image`` is the
#: operation for a picture. A replacement that lands in one of these could only
#: break validation (and then nothing is saved), never express an edit.
_TEXT_SKIP_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "layout",
        "type",
        "icon",
        "icon_strip",
        "tone",
        "density",
        "size",
        "tile_size",
        "align",
        "vertical_align",
        "style",
        "ratio",
        "trend",
        "delta_tone",
        "direction",
        "kind",
        "variant",
        "fit",
        "gap",
        "level",
        "theme",
        "aspect_ratio",
        "transition",
        "hero",
        "hero_tone",
        "overlay",
        "quote_marks",
    }
)

_IMMUTABLE_SLIDE_KEYS: tuple[str, ...] = ("id", "layout")
_IMMUTABLE_BLOCK_KEYS: tuple[str, ...] = ("type",)

#: A dashboard panel's own fields, read off the model: what
#: ``update_block`` on a panel address sets on the panel rather than on its
#: figure. None of them is a figure field there - a panel's chart takes no
#: ``title`` of its own (``PANEL_CHART_TITLE_REFUSAL``) - so the split is exact.
_PANEL_FIELDS: tuple[str, ...] = tuple(name for name in DashboardPanel.model_fields if name != "block")


class PresentationPatchError(ValueError):
    """An operation that cannot be applied as written. Nothing was changed."""


class PresentationPatchNoChangeError(PresentationPatchError):
    """An operation that found nothing to change.

    A ``replace_text`` whose ``find`` occurs nowhere is not a broken call: the
    deck simply already reads that way. The tool answers it as a one-line
    "No change" result - not an error - so four wording probes are four
    no-ops, not four consecutive failures that the loop's failure brake
    answers by withdrawing every tool (the 25 Sep 2026 run). Still a
    ``PresentationPatchError``: nothing was applied, the call is atomic.
    """


def canonical(value: object) -> str:
    """The byte-for-byte comparison form of a spec fragment (sorted keys, no spaces)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(slots=True)
class SlideRef:
    """A slide as the account names it: 1-based position, id, layout, title."""

    index: int
    id: str
    layout: str
    title: str

    def label(self) -> str:
        title = f' "{self.title}"' if self.title else ""
        return f"slide {self.index} [{self.layout}]{title}"


@dataclass(slots=True)
class ImageSwap:
    """One ``replace_image`` as the account names it: the two file ids and every place swapped."""

    find: str
    replace: str
    places: list[str] = field(default_factory=list)
    #: 1-based positions (in the result) of the slides the swap wrote, in order.
    slides: list[int] = field(default_factory=list)
    #: ``(slide id, path on the slide)`` behind each of ``places``, in the same
    #: order: what :func:`reconcile` re-addresses against the validated spec.
    slots: list[tuple[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class PatchResult:
    """The patched spec and the exact account of what the operations touched.

    ``changed`` are pre-existing slides whose content differs from the
    baseline (positions in the RESULT); ``named_unchanged`` are slides an
    operation addressed that came out identical (the edit was already there);
    ``inserted`` and ``moved`` carry result positions, ``deleted`` the position
    the slide had before. ``deck_fields`` are the deck-level keys touched.
    """

    spec: dict
    changed: list[SlideRef] = field(default_factory=list)
    named_unchanged: list[SlideRef] = field(default_factory=list)
    inserted: list[SlideRef] = field(default_factory=list)
    deleted: list[SlideRef] = field(default_factory=list)
    moved: list[tuple[SlideRef, int]] = field(default_factory=list)
    deck_fields: list[str] = field(default_factory=list)
    replacements: int = 0
    operations: list[str] = field(default_factory=list)
    image_swaps: list[ImageSwap] = field(default_factory=list)

    @property
    def pictures_swapped(self) -> int:
        """Places ``replace_image`` changed, across every swap of the edit."""
        return sum(len(swap.places) for swap in self.image_swaps)

    @property
    def touched_ids(self) -> set[str]:
        """Ids the guarantee does NOT cover: content changed, or slide removed."""
        return {ref.id for ref in self.changed} | {ref.id for ref in self.deleted}

    @property
    def unchanged_count(self) -> int:
        """Pre-existing slides that came out byte-identical."""
        total = len(self.spec.get("slides") or [])
        return total - len(self.changed) - len(self.inserted)

    @property
    def is_noop(self) -> bool:
        return not (
            self.changed or self.inserted or self.deleted or self.moved or self.deck_fields
        )


# ── Addressing ───────────────────────────────────────────────────────────────


def _as_position(value: object) -> int | None:
    """A 1-based position from an int / digit string; ``None`` when it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def coerce_revision(value: object) -> int | None:
    """``expected_revision`` as the model sent it, or ``None`` when it is not one.

    Read with the same lenience as a slide number (``_as_position``): an int,
    an integer-valued float or a digit string - a JSON schema ``integer``
    arrives as ``"12"`` from some providers and as ``12.0`` from others, and a
    tool that takes ``slide: 3.0`` but refuses ``expected_revision: 3.0`` on
    every call teaches the model to stop sending the revision, which is the
    stale-slide-number guard gone. A revision starts at 1, so ``0`` is
    not one; ``True`` is not one either.
    """
    position = _as_position(value)
    return position if position is not None and position >= 1 else None


def resolve_slide(slides: Sequence[Mapping[str, Any]], ref: object, *, param: str = "slide") -> int:
    """0-based index of the slide *ref* names: a 1-based position or a slide id."""
    if ref is None or isinstance(ref, bool):
        raise PresentationPatchError(
            f"'{param}' is required: a 1-based slide number or a slide id (both are in the outline)"
        )
    if isinstance(ref, str):
        text = ref.strip()
        for index, slide in enumerate(slides):
            if slide.get("id") == text:
                return index
        if not text.isdigit():
            raise PresentationPatchError(
                f"no slide has id '{text}' (slide ids are listed in get_presentation's outline)"
            )
    position = _as_position(ref)
    if position is None:
        raise PresentationPatchError(f"'{param}' must be a 1-based slide number or a slide id, not {ref!r}")
    if not 1 <= position <= len(slides):
        raise PresentationPatchError(
            f"slide {position} does not exist (the deck has {len(slides)} slides, numbered 1-{len(slides)})"
        )
    return position - 1


@dataclass(slots=True)
class _Slot:
    """Where a block lives: a list position, or a dict key (``footer_block``,
    a dashboard panel's ``block`` - then ``panel`` is that panel)."""

    parent: list | dict
    key: int | str
    panel: dict | None = None

    def exists(self) -> bool:
        if isinstance(self.parent, list):
            return isinstance(self.key, int) and 0 <= self.key < len(self.parent)
        return self.parent.get(self.key) is not None

    def get(self) -> Any:
        return self.parent[self.key]

    def set(self, value: Any) -> None:
        self.parent[self.key] = value

    def insert(self, value: Any) -> None:
        if isinstance(self.parent, list):
            self.parent.insert(self.key, value)
        else:
            self.parent[self.key] = value

    def delete(self) -> None:
        if isinstance(self.parent, list):
            del self.parent[self.key]
        else:
            self.parent.pop(self.key, None)


def _nested_container(node: Any, path: str) -> list | dict:
    """The list a further address segment indexes inside *node*."""
    if isinstance(node, list):  # a column
        return node
    if isinstance(node, dict):
        if isinstance(node.get("children"), list):
            return node["children"]
        if node.get("type") == "columns" and isinstance(node.get("columns"), list):
            return node["columns"]
        if node.get("type") == "comparison":
            return node  # the next segment picks 'left' / 'right'
        raise PresentationPatchError(
            f"block '{path}' ({node.get('type') or 'unknown type'}) has no nested blocks to address"
        )
    raise PresentationPatchError(f"'{path}' is not a block")


def _is_footer_address(address: object) -> bool:
    return str(address).strip().lower() in ("footer", "footer_block")


def _dashboard_panels(slide: Mapping[str, Any]) -> list | None:
    """The panels of a dashboard slide - its body - or ``None``."""
    panels = slide.get("panels")
    return panels if isinstance(panels, list) else None


def _no_body_blocks(slide: Mapping[str, Any]) -> PresentationPatchError:
    """The refusal of a block address on a layout whose content is slide fields."""
    layout = slide.get("layout")
    model = slide_model(str(layout))
    footer = "; its footer_block is block 'footer'" if model is not None and "footer_block" in model.model_fields else ""
    return PresentationPatchError(
        f"a {layout} slide has no body blocks - its content is slide fields: change them with "
        f"update_slide, or the whole slide with replace_slide{footer}"
    )


def _panel_position(panels: list, segment: str, *, allow_end: bool = False) -> int:
    """The 1-based panel *segment* names, checked against the dashboard's panels."""
    position = _as_position(segment)
    if position is None:
        raise PresentationPatchError(f"'{segment}' is not a 1-based panel position (block 'N' is panel N)")
    limit = len(panels) + (1 if allow_end else 0)
    if not 1 <= position <= limit:
        extra = f", or {len(panels) + 1} to add one at the end" if allow_end else ""
        raise PresentationPatchError(
            f"panel {position} does not exist: the dashboard has {len(panels)} panel(s), "
            f"numbered 1-{len(panels)}{extra}"
        )
    return position


def resolve_block_slot(slide: dict, address: object, *, allow_end: bool = False) -> _Slot:
    """The slot the block *address* names inside *slide* (see the module docstring).

    With ``allow_end`` the last position may be one past the end of its
    container (an insert that appends); otherwise it must name an existing
    block.
    """
    if address is None or isinstance(address, bool):
        raise PresentationPatchError(
            "'block' is required: a 1-based block position such as '3' (on a dashboard, "
            "panel 3), '2.3' (column 2, block 3), '3.2' (nested block), or 'footer'"
        )
    text = str(address).strip()
    if _is_footer_address(text):
        return _Slot(slide, "footer_block")
    segments = [part.strip() for part in text.split(".") if part.strip()]
    if not segments:
        raise PresentationPatchError(f"'{text}' is not a block address")

    columns = slide.get("columns")
    blocks = slide.get("blocks")
    panels = _dashboard_panels(slide)
    walked: list[str] = []
    start = 0
    if isinstance(columns, list) and columns:
        parent: list | dict = columns
        if len(segments) == 1:
            raise PresentationPatchError(
                f"this slide uses columns: address a block as 'column.position' (e.g. '1.{segments[0]}')"
            )
    elif isinstance(blocks, list):
        parent = blocks
    elif panels is not None:
        position = _panel_position(panels, segments[0])
        panel = panels[position - 1]
        if not isinstance(panel, dict):
            raise PresentationPatchError(f"panel {position} is not a panel")
        if len(segments) == 1:
            return _Slot(panel, "block", panel=panel)
        # A figure nests nothing today; the walk below says so by name.
        walked.append(segments[0])
        parent = _nested_container(panel.get("block"), segments[0])
        start = 1
    else:
        raise _no_body_blocks(slide)

    for depth, segment in enumerate(segments[start:], start=start):
        walked.append(segment)
        path = ".".join(walked)
        last = depth == len(segments) - 1
        if isinstance(parent, dict):
            # Only a comparison block lands here: its sides are keys.
            if segment not in ("left", "right"):
                raise PresentationPatchError(
                    f"'{path}': a comparison block is addressed with 'left' or 'right', not '{segment}'"
                )
            side = parent.get(segment)
            if not isinstance(side, dict) or not isinstance(side.get("blocks"), list):
                raise PresentationPatchError(f"'{path}' has no blocks")
            if last:
                raise PresentationPatchError(f"'{path}' names a comparison panel; add the block position ('{path}.1')")
            parent = side["blocks"]
            continue
        position = _as_position(segment)
        if position is None:
            raise PresentationPatchError(f"'{segment}' in '{text}' is not a 1-based block position")
        if last:
            if parent and all(isinstance(item, list) for item in parent):
                # The final segment landed on the ``columns`` list of a
                # columns BLOCK: its items are columns, not blocks.
                raise PresentationPatchError(
                    f"'{path}' names a column of a columns block, not a block; "
                    f"add the block position ('{path}.1')"
                )
            limit = len(parent) + (1 if allow_end else 0)
            if not 1 <= position <= limit:
                have = f"1-{len(parent)}" if parent else "none yet"
                extra = f", or {len(parent) + 1} to append" if allow_end and parent else ""
                raise PresentationPatchError(
                    f"'{path}' is out of range: positions available here are {have}{extra}"
                )
            return _Slot(parent, position - 1)
        if not 1 <= position <= len(parent):
            raise PresentationPatchError(f"'{path}' is out of range (positions available: 1-{len(parent)})")
        parent = _nested_container(parent[position - 1], path)
    raise PresentationPatchError(f"'{text}' does not name a block")  # pragma: no cover - loop returns


# ── Operations ───────────────────────────────────────────────────────────────


class _Tracker:
    """What the operations touched, keyed by slide id (stable across moves)."""

    def __init__(self, baseline: Mapping[str, Any], *, slide_cap: int = DEFAULT_SLIDE_CAP) -> None:
        slides = baseline.get("slides") or []
        #: How many slides the deck may hold: the template's
        #: ``max_slides``, the one bound ``insert_slide`` refuses at.
        self.slide_cap = clamp_slide_cap(slide_cap)
        self.original_slides: dict[str, Mapping[str, Any]] = {}
        self.original_index: dict[str, int] = {}
        for index, slide in enumerate(slides, start=1):
            slide_id = slide.get("id") if isinstance(slide, Mapping) else None
            if not isinstance(slide_id, str) or not slide_id:
                raise PresentationPatchError(
                    f"slide {index} has no id - validate the spec before patching it"
                )
            if slide_id in self.original_slides:
                raise PresentationPatchError(f"slide id '{slide_id}' is duplicated - validate the spec first")
            self.original_slides[slide_id] = slide
            self.original_index[slide_id] = index
        self.touched: dict[str, list[str]] = {}
        self.inserted: list[str] = []
        self.deleted: list[SlideRef] = []
        self.moved: dict[str, int] = {}
        self.deck_fields: list[str] = []
        self.replacements = 0
        self.operations: list[str] = []
        #: ``(find, replace, [(slide id, holder, key)])`` per replace_image: the
        #: slots written, accounted at the end from the final spec - a
        #: later operation of the same call may have moved, rewritten or removed them.
        self.image_swaps: list[tuple[str, str, list[tuple[str, dict, str]]]] = []
        #: ``(id(holder), key) -> (holder, file id)``: what a picture slot showed
        #: when the call started, or before its first swap for a holder an
        #: operation of the call created. The holder is kept so its id cannot be
        #: reused by another object; a slot showing that file again at the end
        #: was swapped back, not swapped.
        self.slot_origins: dict[tuple[int, str], tuple[dict, str]] = {}

    def note_picture_slots(self, work: Mapping[str, Any]) -> None:
        """Record what every picture slot of *work* shows before any operation runs."""
        found: list[tuple[dict, str, str]] = []
        for slide in work.get("slides") or []:
            _picture_slots(slide, None, "", found)
        for holder, key, _path in found:
            self.slot_origins[(id(holder), key)] = (holder, canonical_file_reference(holder[key]))

    def touch(self, slide: Mapping[str, Any], op: str) -> None:
        slide_id = str(slide.get("id"))
        if slide_id in self.inserted:
            return
        self.touched.setdefault(slide_id, []).append(op)

    def deck_field(self, name: str) -> None:
        if name not in self.deck_fields:
            self.deck_fields.append(name)

    def fresh_id(self, slides: Sequence[Mapping[str, Any]]) -> str:
        taken = {s.get("id") for s in slides} | set(self.original_slides)
        while True:
            candidate = uuid.uuid4().hex[:12]
            if candidate not in taken:
                return candidate

    def result(self, spec: dict) -> PatchResult:
        slides = spec.get("slides") or []
        refs: dict[str, SlideRef] = {}
        for index, slide in enumerate(slides, start=1):
            refs[str(slide.get("id"))] = SlideRef(
                index=index,
                id=str(slide.get("id")),
                layout=str(slide.get("layout")),
                title=_slide_title(slide),
            )
        changed: list[SlideRef] = []
        named_unchanged: list[SlideRef] = []
        for slide_id in self.touched:
            ref = refs.get(slide_id)
            if ref is None:
                continue  # touched, then deleted
            current = slides[ref.index - 1]
            if canonical(current) != canonical(self.original_slides[slide_id]):
                changed.append(ref)
            else:
                named_unchanged.append(ref)
        inserted = [refs[slide_id] for slide_id in self.inserted if slide_id in refs]
        moved = [
            (refs[slide_id], old_index)
            for slide_id, old_index in self.moved.items()
            if slide_id in refs and refs[slide_id].index != old_index
        ]
        image_swaps = self._surviving_swaps(spec, refs)
        return PatchResult(
            spec=spec,
            changed=sorted(changed, key=lambda r: r.index),
            named_unchanged=sorted(named_unchanged, key=lambda r: r.index),
            inserted=sorted(inserted, key=lambda r: r.index),
            deleted=list(self.deleted),
            moved=sorted(moved, key=lambda m: m[0].index),
            deck_fields=list(self.deck_fields),
            replacements=self.replacements,
            operations=list(self.operations),
            image_swaps=image_swaps,
        )

    def _surviving_swaps(self, spec: dict, refs: Mapping[str, SlideRef]) -> list[ImageSwap]:
        """Each ``replace_image`` as the FINAL spec has it.

        A slot counts only if it is still in the deck and still holds the
        replacement - a later ``replace_block`` / ``replace_slide`` /
        ``delete_*`` or a second swap in the same call undoes it, and an undone
        swap is neither reported nor counted. A slot several swaps wrote is the
        LAST one's (the swap that stands), and a slot showing what it showed
        before the call (A -> X, then X -> A) was not swapped at all. The saved
        slides are the only references the writer is asked to vouch for, so an
        account naming one they do not show would describe a file nobody
        vouched for. Each place is addressed where the picture ends up (a later
        insert shifts it).
        """
        slides = spec.get("slides") or []
        unit = unit_noun(spec, 1)
        # Every picture slot of the slides a swap wrote, by holder identity:
        # where it is now and the file it shows.
        current: dict[tuple[int, str], tuple[int, str, str, str]] = {}
        written_slides = (slide_id for _find, _replace, written in self.image_swaps for slide_id, _h, _k in written)
        for slide_id in dict.fromkeys(written_slides):
            ref = refs.get(slide_id)
            if ref is None:
                continue
            found: list[tuple[dict, str, str]] = []
            _picture_slots(slides[ref.index - 1], None, "", found)
            for holder, key, path in found:
                current[(id(holder), key)] = (ref.index, slide_id, path, canonical_file_reference(holder[key]))
        claimed: set[tuple[int, str]] = set()
        swaps: list[ImageSwap] = []
        for find, replace, written in reversed(self.image_swaps):
            kept: list[tuple[int, str, str]] = []
            for _slide_id, holder, key in written:
                slot = (id(holder), key)
                if slot in claimed:
                    continue
                claimed.add(slot)
                place = current.get(slot)
                origin = self.slot_origins.get(slot)
                if place is None or place[3] != replace or (origin is not None and origin[1] == replace):
                    continue
                kept.append(place[:3])
            if kept:
                swaps.append(_image_swap(find, replace, kept, slides, unit))
        swaps.reverse()
        return swaps


def _image_swap(find: str, replace: str, kept: list[tuple[int, str, str]], slides: Sequence, unit: str) -> ImageSwap:
    """An :class:`ImageSwap` of the ``(1-based slide, slide id, path)`` places kept, in deck order."""
    kept = sorted(kept, key=lambda place: place[0])
    return ImageSwap(
        find=find,
        replace=replace,
        places=[f"{unit} {index} {_place(slides[index - 1], path)}" for index, _slide_id, path in kept],
        slides=list(dict.fromkeys(index for index, _slide_id, _path in kept)),
        slots=[(slide_id, path) for _index, slide_id, path in kept],
    )


def _value_at(node: Any, path: str) -> Any:
    """The value at the dotted *path* inside *node*, or ``None``."""
    for segment in path.split("."):
        if isinstance(node, list) and segment.isdigit() and int(segment) < len(node):
            node = node[int(segment)]
        elif isinstance(node, Mapping):
            node = node.get(segment)
        else:
            return None
    return node


def _readdress_swaps(swaps: Sequence[ImageSwap], patched: Mapping[str, Any]) -> list[ImageSwap]:
    """The swaps addressed in the VALIDATED spec.

    The validator keeps a v2 slide's shape, so a place is normally where the
    engine left it; an operation that wrote a RETIRED v1 slide gets it
    stored as its v2 upgrade, where the picture sits elsewhere - found there by
    its file id, one place per place swapped, never one that was not.
    """
    slides = [slide for slide in patched.get("slides") or [] if isinstance(slide, Mapping)]
    position = {str(slide.get("id")): index for index, slide in enumerate(slides, start=1)}
    unit = unit_noun(dict(patched), 1)
    readdressed: list[ImageSwap] = []
    for swap in swaps:
        kept: list[tuple[int, str, str]] = []
        moved: list[tuple[int, str]] = []
        for slide_id, path in swap.slots:
            index = position.get(slide_id)
            if index is None:
                continue
            value = _value_at(slides[index - 1], path)
            if isinstance(value, str) and canonical_file_reference(value) == swap.replace:
                kept.append((index, slide_id, path))
            else:
                moved.append((index, slide_id))
        for index, slide_id in moved:
            found: list[tuple[dict, str, str]] = []
            _picture_slots(slides[index - 1], swap.replace, "", found)
            taken = {(kept_id, kept_path) for _i, kept_id, kept_path in kept}
            spare = [path for _holder, _key, path in found if (slide_id, path) not in taken]
            if spare:
                kept.append((index, slide_id, spare[0]))
        if kept:
            readdressed.append(_image_swap(swap.find, swap.replace, kept, slides, unit))
    return readdressed


def _slide_title(slide: Mapping[str, Any]) -> str:
    for key in ("title", "subtitle", "label"):
        value = slide.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _require_object(value: object, param: str, what: str) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise PresentationPatchError(f"'{param}' must be a JSON object ({what}), not a string") from None
    if not isinstance(value, dict) or not value:
        raise PresentationPatchError(f"'{param}' must be a non-empty object: {what}")
    return value


def _merge(target: dict, fields: dict, *, immutable: Sequence[str], replace_op: str) -> None:
    for key in immutable:
        if key in fields and fields[key] != target.get(key):
            raise PresentationPatchError(f"'{key}' cannot be changed here; use {replace_op} instead")
    for key, value in fields.items():
        if key in immutable:
            continue
        if value is None:
            target.pop(key, None)
        else:
            target[key] = value


def _slides(work: dict) -> list:
    return work["slides"]


def _op_update_slide(work: dict, op: dict, tracker: _Tracker) -> None:
    slides = _slides(work)
    index = resolve_slide(slides, op.get("slide"))
    fields = _require_object(op.get("fields"), "fields", "the slide fields to set")
    _merge(slides[index], fields, immutable=_IMMUTABLE_SLIDE_KEYS, replace_op="replace_slide")
    tracker.touch(slides[index], "update_slide")


def _new_slide(content: object, *, param: str) -> dict:
    slide = _require_object(content, param, "a complete slide object with a 'layout' key")
    if not isinstance(slide.get("layout"), str):
        raise PresentationPatchError(f"'{param}' must be a complete slide object with a 'layout' key")
    return dict(slide)


def _op_replace_slide(work: dict, op: dict, tracker: _Tracker) -> None:
    slides = _slides(work)
    index = resolve_slide(slides, op.get("slide"))
    replacement = _new_slide(op.get("content"), param="content")
    # The position keeps its identity: comments anchored to the slide stay,
    # and the account can say "slide 4 changed" rather than "4 removed, 4 added".
    replacement["id"] = slides[index]["id"]
    slides[index] = replacement
    tracker.touch(replacement, "replace_slide")


def _op_insert_slide(work: dict, op: dict, tracker: _Tracker) -> None:
    slides = _slides(work)
    cap = tracker.slide_cap
    if len(slides) >= cap:
        # The refusal names the cap AND how it is raised: a deck at
        # its template's cap is not a deck at the platform's limit.
        units = unit_noun(work, cap)
        raise PresentationPatchError(
            f"the {whole_noun(work)} already has {len(slides)} {units}, its cap of {cap}; "
            f"delete or merge a {unit_noun(work, 1)} before inserting another - "
            f"{slide_cap_hint(cap, units=units)}"
        )
    new_slide = _new_slide(op.get("content"), param="content")
    if op.get("at") is not None and op.get("after") is not None:
        raise PresentationPatchError("pass either 'at' (a position) or 'after' (a slide), not both")
    if op.get("after") is not None:
        position = resolve_slide(slides, op.get("after"), param="after") + 2
    elif op.get("at") is not None:
        at = _as_position(op.get("at"))
        if at is None or not 1 <= at <= len(slides) + 1:
            raise PresentationPatchError(
                f"'at' must be a 1-based position between 1 and {len(slides) + 1} (the end)"
            )
        position = at
    else:
        position = len(slides) + 1
    new_slide["id"] = tracker.fresh_id(slides)
    slides.insert(position - 1, new_slide)
    tracker.inserted.append(new_slide["id"])


def _op_delete_slide(work: dict, op: dict, tracker: _Tracker) -> None:
    slides = _slides(work)
    index = resolve_slide(slides, op.get("slide"))
    if len(slides) == 1:
        raise PresentationPatchError("cannot delete the only slide of the presentation")
    removed = slides.pop(index)
    slide_id = str(removed.get("id"))
    if slide_id in tracker.inserted:
        tracker.inserted.remove(slide_id)
        return
    tracker.touched.pop(slide_id, None)
    tracker.moved.pop(slide_id, None)
    tracker.deleted.append(
        SlideRef(
            index=tracker.original_index.get(slide_id, index + 1),
            id=slide_id,
            layout=str(removed.get("layout")),
            title=_slide_title(removed),
        )
    )


def _op_move_slide(work: dict, op: dict, tracker: _Tracker) -> None:
    slides = _slides(work)
    index = resolve_slide(slides, op.get("slide"))
    to = _as_position(op.get("to"))
    if to is None or not 1 <= to <= len(slides):
        raise PresentationPatchError(f"'to' must be a 1-based position between 1 and {len(slides)}")
    slide = slides.pop(index)
    slides.insert(to - 1, slide)
    slide_id = str(slide.get("id"))
    if slide_id in tracker.original_index:
        tracker.moved.setdefault(slide_id, tracker.original_index[slide_id])


def _block_content(value: object, *, param: str) -> dict:
    block = _require_object(value, param, "a complete block object with a 'type' key")
    if not isinstance(block.get("type"), str):
        raise PresentationPatchError(f"'{param}' must be a complete block object with a 'type' key")
    return dict(block)


def _op_update_block(work: dict, op: dict, tracker: _Tracker) -> None:
    slides = _slides(work)
    index = resolve_slide(slides, op.get("slide"))
    slot = resolve_block_slot(slides[index], op.get("block"))
    if not slot.exists():
        raise PresentationPatchError(f"slide {index + 1} has no block at '{op.get('block')}'")
    block = slot.get()
    if not isinstance(block, dict):
        raise PresentationPatchError(f"'{op.get('block')}' is not a block")
    fields = _require_object(op.get("fields"), "fields", "the block fields to set")
    if slot.panel is not None:
        panel_fields = {key: value for key, value in fields.items() if key in _PANEL_FIELDS}
        fields = {key: value for key, value in fields.items() if key not in _PANEL_FIELDS}
        _merge(slot.panel, panel_fields, immutable=(), replace_op="replace_block")
    _merge(block, fields, immutable=_IMMUTABLE_BLOCK_KEYS, replace_op="replace_block")
    tracker.touch(slides[index], "update_block")


def _op_replace_block(work: dict, op: dict, tracker: _Tracker) -> None:
    slides = _slides(work)
    index = resolve_slide(slides, op.get("slide"))
    slot = resolve_block_slot(slides[index], op.get("block"))
    if not slot.exists():
        raise PresentationPatchError(f"slide {index + 1} has no block at '{op.get('block')}'")
    slot.set(_block_content(op.get("content"), param="content"))
    tracker.touch(slides[index], "replace_block")


def _op_insert_block(work: dict, op: dict, tracker: _Tracker) -> None:
    slides = _slides(work)
    index = resolve_slide(slides, op.get("slide"))
    slide = slides[index]
    content = _block_content(op.get("content"), param="content")
    at = op.get("at")
    panels = _dashboard_panels(slide)
    if panels is not None and (at is None or not _is_footer_address(at)):
        # A dashboard's body is its panels: the block comes in as the figure
        # of a new panel, titled (and widened) with update_block on its address.
        if at is None:
            position = len(panels) + 1
        elif "." in str(at).strip():
            raise PresentationPatchError(
                f"'{at}': a dashboard panel holds one figure, so a block is added as a panel - "
                "'at' is the panel position ('3') or 'footer'"
            )
        else:
            position = _panel_position(panels, str(at).strip(), allow_end=True)
        panels.insert(position - 1, {"block": content})
    elif at is None:
        if isinstance(slide.get("columns"), list) and slide["columns"]:
            raise PresentationPatchError(
                "'at' is required on a columns slide: 'column.position' (e.g. '2.3' = column 2, "
                "position 3) or 'footer'"
            )
        blocks = slide.get("blocks")
        if not isinstance(blocks, list):
            raise _no_body_blocks(slide)
        blocks.append(content)
    else:
        slot = resolve_block_slot(slide, at, allow_end=True)
        if isinstance(slot.parent, dict) and slot.exists():
            raise PresentationPatchError(
                f"slide {index + 1} already has a footer_block; use replace_block with block='footer'"
            )
        slot.insert(content)
    tracker.touch(slide, "insert_block")


def _op_delete_block(work: dict, op: dict, tracker: _Tracker) -> None:
    slides = _slides(work)
    index = resolve_slide(slides, op.get("slide"))
    slot = resolve_block_slot(slides[index], op.get("block"))
    if not slot.exists():
        raise PresentationPatchError(f"slide {index + 1} has no block at '{op.get('block')}'")
    if slot.panel is not None:
        # A panel without its figure is no panel: the panel goes with it.
        panels = slides[index]["panels"]
        panels.pop(next(i for i, panel in enumerate(panels) if panel is slot.panel))
    else:
        slot.delete()
    tracker.touch(slides[index], "delete_block")


def _is_text_field(key: str | None) -> bool:
    return key is None or not (key in _TEXT_SKIP_KEYS or is_file_reference_key(key))


def iter_text_fields(
    node: Any, key: str | None = None, path: tuple[str | int, ...] = ()
) -> Iterator[tuple[tuple[str | int, ...], str]]:
    """Every ``(path, text)`` under *node* that ``replace_text`` reads and rewrites.

    *path* is the walk from *node* down: dict keys and 0-based list indexes.
    A list's items take the key of the field holding the list, so the strings
    of an ``icon_strip`` are skipped like a single ``icon``. The one walk
    behind the occurrence count of ``replace_text`` and behind
    :func:`search_text`, so a search can never miss a field the replacement
    would touch, nor report one it would leave alone.
    """
    if isinstance(node, str):
        if _is_text_field(key):
            yield path, node
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from iter_text_fields(item, key, (*path, index))
    elif isinstance(node, dict):
        for child_key, value in node.items():
            yield from iter_text_fields(value, child_key, (*path, child_key))


def _count_strings(node: Any, find: str, key: str | None) -> int:
    """How often *find* occurs in the text fields under *node*."""
    return sum(text.count(find) for _path, text in iter_text_fields(node, key))


def _replace_in_place(node: Any, find: str, replace: str, key: str | None) -> Any:
    """*node* with *find* replaced in its text fields, its containers rewritten
    where they are: every object of the slide keeps its identity, which is what
    the ``replace_image`` account of the same call follows."""
    if isinstance(node, str):
        return node.replace(find, replace) if _is_text_field(key) else node
    if isinstance(node, list):
        for index, item in enumerate(node):
            node[index] = _replace_in_place(item, find, replace, key)
    elif isinstance(node, dict):
        for child_key, value in node.items():
            node[child_key] = _replace_in_place(value, find, replace, child_key)
    return node


def _op_replace_text(work: dict, op: dict, tracker: _Tracker) -> None:
    find = op.get("find")
    replace = op.get("replace", "")
    if not isinstance(find, str) or not find:
        raise PresentationPatchError("'find' must be a non-empty string (matching is exact and case-sensitive)")
    if not isinstance(replace, str):
        raise PresentationPatchError("'replace' must be a string (\"\" deletes the text)")
    if find == replace:
        # A probe for where the text is, not an edit: answered as "No change"
        # with the read-only path, never as a failure the brake counts.
        raise PresentationPatchNoChangeError(
            f"'find' and 'replace' are identical, so replace_text has nothing to change. To check "
            f"where '{find}' appears (titles, blocks, tables, charts, speaker notes, header and "
            "footer), call search_presentation_text with it as the query: it lists every "
            "occurrence with its slide, block and field, and changes nothing"
        )
    expected = op.get("count")
    if expected is not None and _as_position(expected) is None:
        raise PresentationPatchError("'count' must be the number of occurrences you expect")

    slides = _slides(work)
    scope = op.get("slide")
    targets = [resolve_slide(slides, scope)] if scope is not None else list(range(len(slides)))
    # Counted first, applied after every check: a refused operation writes nothing.
    hits: list[tuple[int, int]] = []
    for index in targets:
        count = _count_strings(slides[index], find, None)
        if count:
            hits.append((index, count))
    deck_hits: list[tuple[str, int]] = []
    if scope is None:
        for name in ("title", "chrome"):
            if name in work:
                count = _count_strings(work[name], find, name)
                if count:
                    deck_hits.append((name, count))
    total = sum(count for _, count in hits) + sum(count for _, count in deck_hits)
    if total == 0:
        where = f"on slide {targets[0] + 1}" if scope is not None else "anywhere in the presentation"
        raise PresentationPatchNoChangeError(
            f"'{find}' was not found {where} (matching is exact and case-sensitive; "
            "search_presentation_text finds it in any case and shows the exact text)"
        )
    places = [f"slide {index + 1} ({count})" for index, count in hits]
    places += [f"deck {name} ({count})" for name, count in deck_hits]
    if expected is not None and _as_position(expected) != total:
        raise PresentationPatchError(
            f"expected {_as_position(expected)} occurrence(s) of '{find}' but found {total}: "
            f"{', '.join(places)}. Pass count={total} to replace them all, or scope the "
            "operation to one slide with 'slide'."
        )
    if expected is None and scope is None and total > 1:
        # The user pointed at nothing in particular and the phrase is in
        # several places: changing them all would be the blind deck-wide edit
        # this tool exists to prevent. Show where it is and make the agent
        # choose - one slide, or every occurrence on purpose.
        raise PresentationPatchError(
            f"'{find}' occurs {total} times: {', '.join(places)}. Scope the operation to "
            f"one slide with 'slide', or pass count={total} to replace every occurrence "
            "on purpose."
        )
    for index, _count in hits:
        _replace_in_place(slides[index], find, replace, None)
        tracker.touch(slides[index], "replace_text")
    for name, _count in deck_hits:
        work[name] = _replace_in_place(work[name], find, replace, name)
        tracker.deck_field(name)
    tracker.replacements += total


# ── Read-only text search ─────────────────────────────────────────────────────

#: Deck-level fields ``replace_text`` reads besides the slides: what
#: :func:`search_text` searches there too.
TEXT_DECK_FIELDS: tuple[str, ...] = ("title", "chrome")

#: Characters of context a search snippet keeps on each side of the match.
_SNIPPET_CONTEXT = 40


@dataclass(slots=True)
class TextMatch:
    """One occurrence :func:`search_text` found.

    ``slide`` is the slide's 1-based position, ``None`` for a deck-level field
    (``title``, ``chrome``); ``block`` the block address ``edit_presentation``
    takes (``"3"``, ``"2.1"``, ``"4.left.1"``, ``"footer"``, a dashboard panel
    ``"2"``), ``None`` for a slide field; ``field`` the JSON path of the string
    inside that block (or slide, or deck), list indexes 0-based. ``text`` is
    the occurrence exactly as stored, ``snippet`` the stored string around it.
    """

    slide: int | None
    slide_id: str | None
    layout: str | None
    block: str | None
    field: str
    text: str
    snippet: str


def _format_field_path(path: Sequence[str | int]) -> str:
    out = ""
    for part in path:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out


def _descend_blocks(node: Any, rest: tuple, address: list[str]) -> tuple[tuple, list[str]]:
    """Walk *rest* down the nested blocks of *node*, extending *address*.

    The containers :func:`resolve_block_slot` steps into: a callout's
    ``children``, a columns BLOCK's columns, a comparison's ``left`` /
    ``right`` blocks. Returns what is left of the path once no further block
    is entered - the field path inside the innermost block.
    """
    while isinstance(node, dict) and rest:
        head = rest[0]
        if head == "children" and len(rest) > 2 and isinstance(rest[1], int):
            address.append(str(rest[1] + 1))
            node, rest = node["children"][rest[1]], rest[2:]
        elif (
            head == "columns"
            and node.get("type") == "columns"
            and len(rest) > 3
            and isinstance(rest[1], int)
            and isinstance(rest[2], int)
        ):
            address += [str(rest[1] + 1), str(rest[2] + 1)]
            node, rest = node["columns"][rest[1]][rest[2]], rest[3:]
        elif (
            head in ("left", "right")
            and node.get("type") == "comparison"
            and len(rest) > 3
            and rest[1] == "blocks"
            and isinstance(rest[2], int)
        ):
            address += [head, str(rest[2] + 1)]
            node, rest = node[head]["blocks"][rest[2]], rest[3:]
        else:
            break
    return rest, address


def locate_text_field(slide: Mapping[str, Any], path: tuple[str | int, ...]) -> tuple[str | None, str]:
    """``(block address, field path)`` of the string at *path* inside *slide*.

    The address is the one ``update_block`` / ``replace_text`` scoping read
    (see the module docstring): a ``blocks`` slide's ``"3"``, a columns
    slide's ``"2.1"``, a dashboard's panel ``"2"`` (its ``title`` is the
    panel's, its figure's fields the block's), ``"footer"``, then nested
    blocks. A string outside any block - a title, the notes, a dashboard tile
    - has no address and its path is from the slide.
    """
    if len(path) >= 2 and path[0] == "blocks" and isinstance(path[1], int):
        return _finish_location(slide["blocks"][path[1]], path[2:], [str(path[1] + 1)])
    if (
        len(path) >= 3
        and path[0] == "columns"
        and isinstance(path[1], int)
        and isinstance(path[2], int)
        and isinstance(slide["columns"][path[1]], list)
    ):
        return _finish_location(slide["columns"][path[1]][path[2]], path[3:], [str(path[1] + 1), str(path[2] + 1)])
    if len(path) >= 2 and path[0] == "panels" and isinstance(path[1], int):
        address = [str(path[1] + 1)]
        if len(path) >= 3 and path[2] == "block":
            return _finish_location(slide["panels"][path[1]]["block"], path[3:], address)
        return ".".join(address), _format_field_path(path[2:])
    if path and path[0] == "footer_block":
        return "footer", _format_field_path(path[1:])
    return None, _format_field_path(path)


def _finish_location(block: Any, rest: tuple, address: list[str]) -> tuple[str, str]:
    rest, address = _descend_blocks(block, rest, address)
    return ".".join(address), _format_field_path(rest)


def text_query_pattern(query: str, *, case_sensitive: bool, whole_word: bool) -> re.Pattern[str]:
    """The compiled search for *query*: literal, optionally case-insensitive.

    ``whole_word`` puts a word boundary on each side of the query that starts
    or ends with a word character, so ``core`` never matches inside
    ``scores`` while ``Q3:`` still matches before a space.
    """
    body = re.escape(query)
    if whole_word:
        if re.match(r"\w", query[:1]):
            body = r"(?<!\w)" + body
        if re.match(r"\w", query[-1:]):
            body += r"(?!\w)"
    return re.compile(body, 0 if case_sensitive else re.IGNORECASE)


def _snippet(text: str, start: int, end: int) -> str:
    left = max(0, start - _SNIPPET_CONTEXT)
    right = min(len(text), end + _SNIPPET_CONTEXT)
    snippet = f"{text[left:start]}«{text[start:end]}»{text[end:right]}"
    snippet = " ".join(snippet.split())
    return f"{'...' if left else ''}{snippet}{'...' if right < len(text) else ''}"


def search_text(
    spec: Mapping[str, Any],
    query: str,
    *,
    case_sensitive: bool = False,
    whole_word: bool = False,
) -> list[TextMatch]:
    """Every occurrence of *query* in the text fields ``replace_text`` would touch.

    Read-only: *spec* is never modified. The fields are those of
    :func:`iter_text_fields` - every slide (titles, blocks at any depth,
    table cells, chart labels and captions, dashboard tiles and panels,
    speaker notes) and the deck's ``title`` and ``chrome`` - and never an id,
    a layout, a block type, an icon, a tone or a file id. Occurrences do not
    overlap, as ``str.count`` counts them: a case-sensitive search that is not
    whole-word finds exactly what ``replace_text`` counts. Slides come first,
    in order, then the deck fields.
    """
    if not isinstance(query, str) or not query:
        raise PresentationPatchError("'query' must be a non-empty string")
    pattern = text_query_pattern(query, case_sensitive=case_sensitive, whole_word=whole_word)
    matches: list[TextMatch] = []
    slides = spec.get("slides") if isinstance(spec.get("slides"), list) else []
    for index, slide in enumerate(slides):
        if not isinstance(slide, dict):
            continue
        for path, text in iter_text_fields(slide):
            for found in pattern.finditer(text):
                block, field_path = locate_text_field(slide, path)
                matches.append(
                    TextMatch(
                        slide=index + 1,
                        slide_id=str(slide.get("id") or "") or None,
                        layout=str(slide.get("layout") or "") or None,
                        block=block,
                        field=field_path,
                        text=found.group(0),
                        snippet=_snippet(text, found.start(), found.end()),
                    )
                )
    for name in TEXT_DECK_FIELDS:
        if name not in spec:
            continue
        for path, text in iter_text_fields(spec[name], name, (name,)):
            for found in pattern.finditer(text):
                matches.append(
                    TextMatch(
                        slide=None,
                        slide_id=None,
                        layout=None,
                        block=None,
                        field=_format_field_path(path),
                        text=found.group(0),
                        snippet=_snippet(text, found.start(), found.end()),
                    )
                )
    return matches


#: Most of the OTHER pictures a scope shows that a "not shown there" answer names.
_MAX_PICTURES_NAMED = 6
#: Most places a count refusal names (a background image can sit on all 100
#: slides); the count itself is always exact.
_MAX_PLACES_NAMED = 12


def _named_places(places: list[str]) -> str:
    named = "; ".join(places[:_MAX_PLACES_NAMED])
    extra = len(places) - _MAX_PLACES_NAMED
    return f"{named}; and {extra} more" if extra > 0 else named


def _file_id_argument(value: object, param: str) -> str:
    """A replace_image file id as the model spelled it, in the form the spec compares."""
    if not isinstance(value, str) or not value.strip():
        raise PresentationPatchError(
            f"'{param}' must be a file id (get_presentation lists every picture of the deck with its file id)"
        )
    return normalize_file_reference(value)


def _picture_slots(node: Any, find: str | None, path: str, found: list[tuple[dict, str, str]]) -> None:
    """``(holder, key, path)`` of every file field under *node* - naming *find*, or any when ``None``."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            if isinstance(value, str) and is_file_reference_key(key):
                if find is None or canonical_file_reference(value) == find:
                    found.append((node, key, here))
            else:
                _picture_slots(value, find, here, found)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _picture_slots(item, find, f"{path}.{index}", found)


def _object_ids(node: Any) -> set[int]:
    """Identity of every object under *node*: what scopes a walk of the slide to one block."""
    ids: set[int] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            ids.add(id(current))
            stack.extend(current.values())
        elif isinstance(current, list):
            ids.add(id(current))
            stack.extend(current)
    return ids


def _place(slide: Mapping[str, Any], path: str) -> str:
    """``block 1 (logo_grid) item 3 "Acme"``: a file field's place on its slide."""
    where, context = describe_location(slide, path.split("."))
    return f'{where} "{context}"' if context else where


def _op_replace_image(work: dict, op: dict, tracker: _Tracker) -> None:
    find = _file_id_argument(op.get("find"), "find")
    replace = _file_id_argument(op.get("replace"), "replace")
    if not is_valid_uuid(replace):
        raise PresentationPatchError(
            f"'replace' must be the file id (a UUID) of the new picture, not {op.get('replace')!r}: "
            "a template asset id from get_template, or the id of a file attached to this conversation"
        )
    if find == replace:
        raise PresentationPatchError("'find' and 'replace' are the same file")
    expected = op.get("count")
    if expected is not None and _as_position(expected) is None:
        raise PresentationPatchError("'count' must be the number of places you expect")
    slides = _slides(work)
    scope = op.get("slide")
    block = op.get("block")
    if block is not None and scope is None:
        raise PresentationPatchError("'block' needs 'slide': the slide the block is on")
    targets = [resolve_slide(slides, scope)] if scope is not None else list(range(len(slides)))
    within: set[int] | None = None
    if block is not None:
        slot = resolve_block_slot(slides[targets[0]], block)
        if not slot.exists():
            raise PresentationPatchError(f"slide {targets[0] + 1} has no block at '{block}'")
        within = _object_ids(slot.get())

    def _slots(index: int, wanted: str | None) -> list[tuple[dict, str, str]]:
        found: list[tuple[dict, str, str]] = []
        _picture_slots(slides[index], wanted, "", found)
        return [slot for slot in found if within is None or id(slot[0]) in within]

    matches = [(index, holder, key, path) for index in targets for holder, key, path in _slots(index, find)]
    total = len(matches)
    if total == 0:
        # Not a failure: the deck simply does not show that file there.
        # The answer names what it does show, so a wrong id is corrected on
        # the next call instead of guessed at again.
        if block is not None:
            where = f"in block {block} of slide {targets[0] + 1}"
        elif scope is not None:
            where = f"on slide {targets[0] + 1}"
        else:
            where = "anywhere in the presentation"
        shown: dict[str, str] = {}
        for index in targets:
            for holder, key, path in _slots(index, None):
                place = f"slide {index + 1} {_place(slides[index], path)}"
                shown.setdefault(canonical_file_reference(holder[key]), place)
        if shown:
            named = [f"`{file_id}` ({place})" for file_id, place in list(shown.items())[:_MAX_PICTURES_NAMED]]
            more = f" and {len(shown) - _MAX_PICTURES_NAMED} more" if len(shown) > _MAX_PICTURES_NAMED else ""
            tail = f"; the pictures shown there are {', '.join(named)}{more}"
        else:
            tail = "; no picture is shown there"
        raise PresentationPatchNoChangeError(f"file `{find}` is not shown {where}{tail}")
    places = [f"slide {index + 1} {_place(slides[index], path)}" for index, _holder, _key, path in matches]
    if expected is not None and _as_position(expected) != total:
        raise PresentationPatchError(
            f"expected {_as_position(expected)} place(s) of file `{find}` but found {total}: "
            f"{_named_places(places)}. Pass count={total} to swap them all, or scope the operation "
            "with 'slide' (and 'block')."
        )
    if expected is None and scope is None and total > 1:
        # The same rule as replace_text: a picture shown in several places is
        # never swapped everywhere blindly - one slide, or every place on purpose.
        raise PresentationPatchError(
            f"file `{find}` is shown in {total} places: {_named_places(places)}. Scope the operation "
            f"to one slide with 'slide' (and 'block'), or pass count={total} to swap every place "
            "on purpose."
        )
    for _index, holder, key, _path in matches:
        tracker.slot_origins.setdefault((id(holder), key), (holder, find))
        holder[key] = replace
    for index in dict.fromkeys(index for index, _holder, _key, _path in matches):
        tracker.touch(slides[index], "replace_image")
    tracker.image_swaps.append(
        (find, replace, [(str(slides[index].get("id")), holder, key) for index, holder, key, _path in matches])
    )


def _op_update_deck(work: dict, op: dict, tracker: _Tracker) -> None:
    fields = _require_object(op.get("fields"), "fields", f"deck fields to set ({', '.join(DECK_FIELDS)})")
    unknown = sorted(set(fields) - set(DECK_FIELDS))
    if unknown:
        raise PresentationPatchError(
            f"update_deck cannot set {', '.join(unknown)}; deck fields are {', '.join(DECK_FIELDS)} "
            "(slides are edited with the slide operations)"
        )
    for name, value in fields.items():
        if name == "chrome" and isinstance(value, dict):
            current = work.get("chrome")
            merged = dict(current) if isinstance(current, dict) else {}
            for key, item in value.items():
                if item is None:
                    merged.pop(key, None)
                elif key == "fields" and isinstance(item, dict):
                    # The template's footer / header variables, one
                    # value each: "mark it Confidential" sets ONE key and
                    # keeps the customer's name; null (or a blank) clears one.
                    # Keys are read through the validator's own normaliser
                    # (``canonical_field_key``), so ``Classification`` clears
                    # or overwrites the stored ``classification`` rather than
                    # landing beside it.
                    fields_merged = merge_deck_fields(merged.get("fields"), item)
                    if fields_merged:
                        merged["fields"] = fields_merged
                    else:
                        merged.pop("fields", None)
                else:
                    merged[key] = item
            work["chrome"] = merged
        elif value is None:
            work.pop(name, None)
        else:
            work[name] = value
        tracker.deck_field(name)


_HANDLERS: dict[str, Callable[[dict, dict, _Tracker], None]] = {
    "update_slide": _op_update_slide,
    "replace_slide": _op_replace_slide,
    "insert_slide": _op_insert_slide,
    "delete_slide": _op_delete_slide,
    "move_slide": _op_move_slide,
    "update_block": _op_update_block,
    "replace_block": _op_replace_block,
    "insert_block": _op_insert_block,
    "delete_block": _op_delete_block,
    "replace_text": _op_replace_text,
    "replace_image": _op_replace_image,
    "update_deck": _op_update_deck,
}
assert tuple(_HANDLERS) == OPERATIONS  # noqa: S101 - import-time contract


def coerce_operations(operations: object) -> list[dict]:
    """The operation list as dicts: accepts a JSON string, a single operation object, or a list."""
    if isinstance(operations, str):
        try:
            operations = json.loads(operations)
        except ValueError:
            raise PresentationPatchError("'operations' must be a JSON array of operation objects") from None
    if isinstance(operations, dict):
        operations = [operations]
    if not isinstance(operations, list) or not operations:
        raise PresentationPatchError(
            "'operations' must be a non-empty array of operation objects, each with an 'op' key "
            f"(one of: {', '.join(OPERATIONS)})"
        )
    if len(operations) > MAX_OPERATIONS:
        raise PresentationPatchError(
            f"{len(operations)} operations in one call; the limit is {MAX_OPERATIONS}. Split the "
            "edit into several calls, or rewrite the deck with generate_presentation."
        )
    result: list[dict] = []
    for position, item in enumerate(operations, start=1):
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except ValueError:
                item = None
        if not isinstance(item, dict):
            raise PresentationPatchError(f"operation {position} is not an object")
        result.append(item)
    return result


def apply_operations(
    baseline: Mapping[str, Any],
    operations: object,
    *,
    slide_cap: int = DEFAULT_SLIDE_CAP,
) -> PatchResult:
    """Apply *operations* in order to a deep copy of *baseline* and account for them.

    *baseline* is a VALIDATED spec (every slide carries an id). Raises
    :class:`PresentationPatchError`, naming the failing operation by its
    position, when an operation cannot be applied as written; the baseline is
    never mutated, so nothing is half-applied. *slide_cap* is how many slides
    the deck may hold (the template's ``max_slides``): ``insert_slide``
    refuses at it, naming it.
    """
    ops = coerce_operations(operations)
    work = copy.deepcopy(dict(baseline))
    slides = work.get("slides")
    if not isinstance(slides, list) or not slides:
        raise PresentationPatchError("the presentation has no slides to edit")
    tracker = _Tracker(baseline, slide_cap=slide_cap)
    if any(op.get("op") == "replace_image" for op in ops):
        tracker.note_picture_slots(work)
    for position, op in enumerate(ops, start=1):
        name = op.get("op")
        if name is None:
            raise PresentationPatchError(
                f"operation {position} has no 'op' key; one of {', '.join(OPERATIONS)}"
            )
        handler = _HANDLERS.get(str(name))
        if handler is None:
            raise PresentationPatchError(
                f"operation {position}: unknown op {name!r}; one of {', '.join(OPERATIONS)}"
            )
        tracker.operations.append(str(name))
        try:
            handler(work, op, tracker)
        except PresentationPatchError as exc:
            # Re-raised with the operation named, as the SAME class: a
            # no-change refusal must still read as one to the tool.
            raise type(exc)(f"operation {position} ({name}): {exc}") from None
    return tracker.result(work)


def reconcile(
    result: PatchResult,
    baseline: Mapping[str, Any],
    patched: Mapping[str, Any],
) -> PatchResult:
    """Re-derive the account of *result* against the VALIDATED *patched* spec.

    The tracker classifies a slide as changed the moment an operation writes
    it, but the validator canonicalises afterwards: a subtitle set to ``" "``
    is dropped, a title is stripped, a list item is coerced — and what
    normalises back to the baseline was never a change. Without this pass a
    mixed edit (one real change, one that normalised away) saves a revision
    whose result and history name a slide that is byte-identical to what it
    was. Positions are kept (validation never reorders); every ref is re-read
    from *patched* so the title shown is the normalised one; ``deck_fields``
    keeps only the keys whose validated value differs; deletions stand as
    performed. Called by the tool right after ``validate_spec(result.spec)``,
    both arguments being validator output.
    """
    baseline_by_id = {
        str(slide.get("id")): slide
        for slide in baseline.get("slides") or []
        if isinstance(slide, Mapping)
    }
    slides = [slide for slide in patched.get("slides") or [] if isinstance(slide, Mapping)]
    refs = {
        str(slide.get("id")): SlideRef(
            index=index,
            id=str(slide.get("id")),
            layout=str(slide.get("layout")),
            title=_slide_title(slide),
        )
        for index, slide in enumerate(slides, start=1)
    }
    named = [ref.id for ref in result.changed] + [ref.id for ref in result.named_unchanged]
    changed: list[SlideRef] = []
    named_unchanged: list[SlideRef] = []
    for slide_id in named:
        ref = refs.get(slide_id)
        original = baseline_by_id.get(slide_id)
        if ref is None or original is None:
            continue
        if canonical(slides[ref.index - 1]) != canonical(original):
            changed.append(ref)
        else:
            named_unchanged.append(ref)
    inserted = [refs[ref.id] for ref in result.inserted if ref.id in refs]
    moved = [
        (refs[ref.id], old_index)
        for ref, old_index in result.moved
        if ref.id in refs and refs[ref.id].index != old_index
    ]
    deck_fields = [
        name for name in result.deck_fields if canonical(patched.get(name)) != canonical(baseline.get(name))
    ]
    return PatchResult(
        spec=dict(patched),
        changed=sorted(changed, key=lambda r: r.index),
        named_unchanged=sorted(named_unchanged, key=lambda r: r.index),
        inserted=sorted(inserted, key=lambda r: r.index),
        deleted=list(result.deleted),
        moved=sorted(moved, key=lambda m: m[0].index),
        deck_fields=deck_fields,
        replacements=result.replacements,
        operations=list(result.operations),
        image_swaps=_readdress_swaps(result.image_swaps, patched),
    )


def verify_untouched(
    baseline: Mapping[str, Any],
    result: Mapping[str, Any],
    touched_ids: set[str],
) -> list[str]:
    """Prove every slide outside *touched_ids* survived byte-identical.

    Compares each baseline slide with the result slide carrying the same id,
    as canonical JSON; a moved slide compares equal (identity is the id, not
    the position). Returns a description of every violation — an empty list
    is the guarantee the tool result states. Both specs must be the output of
    ``validate_spec`` so normalisation cannot masquerade as a change.
    """
    result_by_id = {str(s.get("id")): s for s in result.get("slides") or [] if isinstance(s, Mapping)}
    problems: list[str] = []
    for index, slide in enumerate(baseline.get("slides") or [], start=1):
        slide_id = str(slide.get("id")) if isinstance(slide, Mapping) else ""
        if slide_id in touched_ids:
            continue
        current = result_by_id.get(slide_id)
        if current is None:
            problems.append(f"slide {index} ({slide_id}) disappeared")
        elif canonical(current) != canonical(slide):
            problems.append(f"slide {index} ({slide_id}) changed although no operation named it")
    return problems


def summarize_patch(result: PatchResult, spec: Mapping[str, Any] | None = None) -> str:
    """One history line for the version record when the agent gave none.

    *spec* (the patched deck) supplies the unit noun: a portrait page's history
    says "Edited page 1", never "slide" (the kind contract of
    ``presentation_spec.unit_noun``).
    """

    def unit(count: int) -> str:
        return unit_noun(dict(spec) if spec is not None else None, count)

    parts: list[str] = []
    if result.changed:
        numbers = ", ".join(str(ref.index) for ref in result.changed)
        parts.append(f"Edited {unit(len(result.changed))} {numbers}")
    if result.inserted:
        numbers = ", ".join(str(ref.index) for ref in result.inserted)
        parts.append(f"Inserted {unit(len(result.inserted))} {numbers}")
    if result.deleted:
        numbers = ", ".join(str(ref.index) for ref in result.deleted)
        parts.append(f"Deleted {unit(len(result.deleted))} {numbers}")
    if result.moved:
        parts.append("Moved " + ", ".join(f"{unit(1)} {old} to {ref.index}" for ref, old in result.moved))
    if result.deck_fields:
        parts.append("Changed " + ", ".join(result.deck_fields))
    if result.replacements and "replace_text" in result.operations:
        parts.append(f"{result.replacements} text replacement{'s' if result.replacements > 1 else ''}")
    if result.pictures_swapped:
        parts.append(f"{result.pictures_swapped} picture{'s' if result.pictures_swapped > 1 else ''} swapped")
    return "; ".join(parts) or "No change"


_RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d*)$")


def parse_slide_selection(selection: object, slides: Sequence[Mapping[str, Any]]) -> list[int]:
    """0-based indices for a ``slides`` selector: ``"12"``, ``"3-7"``, ``"30-"``,
    ``"1,4,9-11"``, ``"all"``, or slide ids — in the order given, deduplicated.

    Accepts an int, a string, or a list of either. An empty selection is an
    empty list. Raises :class:`PresentationPatchError` for a token that names
    nothing.
    """
    if selection is None:
        return []
    if isinstance(selection, bool):
        raise PresentationPatchError("'slides' must be slide numbers, ranges or ids")
    if isinstance(selection, int):
        tokens = [str(selection)]
    elif isinstance(selection, str):
        tokens = [token for token in re.split(r"[,\s;]+", selection.strip()) if token]
    elif isinstance(selection, list):
        tokens = []
        for item in selection:
            tokens.extend(token for token in re.split(r"[,\s;]+", str(item).strip()) if token)
    else:
        raise PresentationPatchError("'slides' must be slide numbers, ranges or ids")
    count = len(slides)
    picked: list[int] = []

    def add(index: int) -> None:
        if index not in picked:
            picked.append(index)

    for token in tokens:
        if token.lower() == "all":
            for index in range(count):
                add(index)
            continue
        match = _RANGE_RE.match(token)
        if match:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else count
            if not 1 <= start <= end <= count:
                raise PresentationPatchError(
                    f"range '{token}' is out of bounds (the deck has {count} slides, numbered 1-{count})"
                )
            for index in range(start - 1, end):
                add(index)
            continue
        add(resolve_slide(slides, token, param="slides"))
    return picked


def describe_operations() -> str:
    """The operation catalog, one line each - the tool description embeds it."""
    return "\n".join(f"- {name} {contract}" for name, contract in OPERATION_CONTRACTS.items())
