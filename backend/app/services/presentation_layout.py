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

"""Layout check of a saved deck.

``validate_spec`` guarantees a deck is well-formed; it cannot tell whether six
cards and a callout FIT on a 1920x1080 slide, because that is a property of
pixels, fonts and wrapping, not of the JSON. Until now the first one to find out
was the presenter: the body painted under its footer block and the chrome, a
table lost its last rows, a badge ran past its pill. Prompting the agent to "keep
slides light" does not change that - it cannot see the slide it wrote.

Two deterministic answers, no prompt text:

* On screen, ``fit.ts`` shrinks a body that does not fit so nothing overlaps
  (PowerPoint's autofit) and publishes the ratio on the DOM.
* Here, the deck is rendered in the export's headless Chromium right after the
  save, ``LAYOUT_AUDIT_JS`` measures every slide (that ratio, glyphs cut by a
  clipping box, text past the canvas), and the observations become findings the
  agent can act on: WHICH slide, WHICH block, WHAT to do. They ride the
  ``generate_presentation`` result, so the agent that just saved a too-dense
  slide is told so in the same turn and trims it before answering - the
  human never has to notice.

The check is advisory and bounded. It never fails a save: a render that times
out, a missing browser or a disabled flag produce a one-line note instead of
findings, and the deck is saved either way. A shrunk slide is still a valid
slide; it is just one the audience reads with difficulty.

A check is about ONE revision. The render page loads the deck's current row,
not the caller's snapshot, so a save landing from a collaborator between the two
would have the findings of revision N+1 read against the titles of revision N;
the page therefore publishes the revision it rendered and a mismatch is a
skipped check, never a report about a deck nobody has. The same revision keys a
short-lived cache of a successful report: a read-back right after a save, or the
panel's re-check of an unchanged deck, must not launch a second browser to
measure pixels that cannot have changed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import typing
import uuid
from dataclasses import dataclass, fields
from typing import Literal

from app.core.config import settings
from app.core.redis import redis_client
from app.core.utils import format_exception_message
from app.services.chart_export import VALUE_LABEL_SIZE_MIN_PX
from app.services.presentation_spec import (
    ORG_CHART_READABLE_NODES,
    QUOTE_SIZES,
    STATEMENT_MAX_LINES,
    Size,
    org_chart_hints,
    unit_noun,
)

logger = logging.getLogger(__name__)

#: Longer than an interactive turn should wait; shorter than the render's own
#: ready timeout, so a deck that renders slowly is skipped rather than failed.
LAYOUT_CHECK_TIMEOUT_SECS = 45.0
#: How long a successful report of one (deck, user, revision) is kept. A
#: revision's pixels never change; the TTL only bounds a theme edit that does
#: not bump the deck (a template container re-styled underneath it).
LAYOUT_CHECK_CACHE_TTL_SECS = 900
#: Findings shown to the agent per check - the worst first; the count of the
#: rest is stated so nothing is silently dropped.
MAX_REPORTED_FINDINGS = 8
#: A body shrunk less than this is within what a reader forgives (a line of
#: slack); below it the slide is dense, and below the second threshold it is
#: overloaded - text at two thirds of its size is not readable from the room.
FIT_DENSE_BELOW = 0.92
FIT_OVERLOADED_BELOW = 0.75
#: ``FIT_MIN`` in ``fit.ts``: the renderer never scales a body below this (the
#: text would be unreadable), it clips what still does not fit instead. A ratio
#: AT the floor therefore means "and part of the body is cut off"; the finding
#: says so, with the cut the audit measured. Mirrored, not imported - a test
#: pins the two values together.
FIT_FLOOR = 0.35
#: A cut this deep (canvas px) is a row of a table or a line of text, not a
#: descender; smaller cuts are still reported as dense.
CUT_OVERLOADED_PX = 40
#: The other direction: a content slide whose body leaves more than
#: this share of its height empty below the last block reads as unfinished -
#: the six-item grid packed under the title with the bottom third bare. Every
#: other finding measures excess; this one measures deficit, on the BODY (the
#: room between the title and the footer block), never on a slide whose blocks
#: were placed on purpose (``vertical_align`` center / end - unless
#: ``distribute`` overrides the placement, as it does in the renderer) or
#: already shrunk, cut or overflowing - never both directions on one slide.
#: Blocks are sized to their content: a card or a grid is never
#: stretched to the floor to hide the room left, so the remedies never name
#: ``fill`` or ``distribute``. A slide closed by a ``footer_block`` ends on
#: purpose and is never sparse; a body left up to half empty reads as a
#: slide that ends early, not as an unfinished one. The threshold is the
#: share of the BODY; the message restates it as the share of the SLIDE,
#: which is what the audience sees. The remedies are read off the slide
#: (:func:`sparse_remedy`), never recited.
SPARSE_ABOVE = 0.50
#: Balance. A content body in columns whose columns' blocks END more than
#: this share of the body apart (the tallest column's last block against the
#: shortest's) reads as one finished column beside an unfinished one: four KPI
#: tiles on the left, one short card on the right. ``uneven``, advisory.
UNEVEN_ABOVE = 0.20
#: A content body of ONE block, top-aligned, that leaves more than this share
#: of the body bare: a lone card or picture floating under the title.
#: ``floating``, advisory - the remedy is a placement (``vertical_align:
#: center``) or the content that carries the block, never a stretch. A filler
#: (a ``fill`` chart, figure or diagram) takes the room and is never measured
#: bare; a lone statement quote is centred by the renderer.
FLOATING_ABOVE = 0.30
#: A content body of SEVERAL blocks, top-aligned, that ends in the top two
#: thirds of its room: more than this share of the body bare under it, but
#: not yet ``SPARSE_ABOVE``. ``top_heavy``, advisory - the bottom third reads
#: unfinished; the remedy is a placement (``vertical_align: center``) or the
#: content that carries the claim, never a stretch.
TOP_HEAVY_ABOVE = 1 / 3
#: A body that ASKS to fill its room - ``vertical_align: stretch``,
#: ``distribute: true`` or a ``fill`` block - and still leaves more than this
#: share of the body bare under its last block: the request was not honoured.
#: ``unfilled``, graded ``dense`` (never advisory): the slide does not look
#: as it was asked to. It is the content's limit, not a renderer defect:
#: blocks are sized to their content and a ``fill`` spread is bounded
#: (``FILL_BLOCK_TYPES`` in ``presentation_spec``), so short columns never
#: reach the floor; the remedy is ``vertical_align: center`` or more content.
#: The same grade for a ``fill`` table or a map in a
#: dashboard panel that leaves more than this share of the panel empty
#: (``panel_fill`` observation).
UNFILLED_ABOVE = 0.10
#: A card title, a persona or tier name, a profile name: one line by design.
#: Drawn on more lines than this, it reads as a paragraph where a label was
#: meant. ``wrapped``, advisory.
CARD_TITLE_MAX_LINES = 1
#: A slide, panel or chart title and a chart legend: one line (one row of
#: entries) by design. More reads as a paragraph where a label was meant, and
#: a legend's last entry alone on a row of its own. ``wrap_title`` and
#: ``wrap_legend``, advisory.
ONE_LINE_MAX_LINES = 1
#: Logo walls. A logo mark drawn under this height (canvas px, as the
#: room sees it) is unreadable - a wordmark's letters are a few pixels tall.
LOGO_MIN_HEIGHT_PX = 24
#: A logo field (a grid's, a wall card's) whose logos, taken as packed rows
#: (the widest multi-logo row's span times the rows' height), cover less than
#: this share of it leaves them lost in empty space: rows spread thin over a
#: tall box, or a few small logos in a wide one. A field of a single logo is
#: not measured - it is placed, not lost. The field is what is measured: a
#: grid that does not ``fill`` hugs its own rows - except when it ends a
#: ``fill`` callout, where it is measured against the card.
LOGO_MIN_COVERAGE = 0.35
#: A logo painted at a WCAG contrast ratio under this against the surface
#: under it is there and unseen: a white one-colour mark on a pale light
#: slide, a dark mark on a dark card. 1.5:1 is far under the 3:1 a graphic
#: needs to be read; under it nobody sees the mark at all.
LOGO_MIN_CONTRAST = 1.5
#: Charts. A chart canvas painted under this height (canvas px, as
#: the room sees it) is a plot nobody reads, and at 0 there is no plot at all
#: - the legend and the caption over nothing. The renderer never lays a chart
#: that is handed the room (``fill``, ``size: full``) out shorter: the
#: ``min-height`` of its canvas in ``presentation.css`` is this value, which a
#: test pins. One still painted under it was shrunk with its body, or drawn
#: where nothing gives it room; under half of it, it is overloaded.
CHART_MIN_HEIGHT_PX = 160
#: The tallest canvas a slide gives a chart: ``size: lg``, the
#: ``--deck-chart-h`` of ``.deck-chart[data-size="lg"]`` in ``presentation.css``
#: and ``TALLEST_CHART_CANVAS`` in ``chart-layout.ts`` (a test pins all three).
#: The drawing plans a chart whose stacks hide values at this height too, and
#: more height is recommended only when that plan writes them all.
CHART_TALLEST_CANVAS_PX = 640

#: ``lines``: a ``statement`` quote that wraps past
#: ``STATEMENT_MAX_LINES`` at the size it is drawn in. It fits - nothing is cut,
#: nothing shrinks - but a statement is read in one breath, and six lines of
#: headline type are a paragraph shouted at the room. The page reports the line
#: count of every statement; the threshold is graded here.
#:
#: ``columns``: a ``columns`` block of four keeps them side by side
#: whenever its widest column's content fits a quarter of the block, and wraps
#: them 2 x 2 only when it does not. The wrap is the renderer's fallback, not
#: the layout the deck asked for, so it is reported as dense, with both widths.
#:
#: ``chart``: a chart drawn under ``CHART_MIN_HEIGHT_PX``.
#:
#: ``values``: a ``chart`` that writes fewer values than its block asks
#: for (``show_values``, on by default). The renderer sets a label smaller and
#: turns it on its side (a line's or an area's, writes them alternately above
#: and below the points) before it gives one up, so what is left is a chart
#: with truly no room for its numbers: dense, with the remedy the reason
#: names - except segments of a stack too thin to hold their value and with no
#: room past their stack either, whose totals are written, which is
#: advisory and recommends only the room the drawing found would write them.
#: A slide already told a chart is too short (``chart``) is not also told its
#: values lack the height. A chart that keeps its values upright at the size
#: asked (``value_label_fit`` upright) leaves out the ones that would
#: touch another, reason ``upright``. A horizontal bar or stacked bar chart
#: is told what its rows lack in height and its values past the bars
#: in width, and offered ``value_label_fit`` stagger; a stack it kept upright
#: is offered stagger for the segments it left out (the observation's ``fit``).
#: A ``map`` writes a country's value inside it or on hover, so the one
#: it leaves out is a country beyond its ``region`` - reason ``region``, the
#: codes named.
#:
#: ``rotated``: a ``chart`` whose values are written turned on their
#: side - even at the smallest size of its text scale (``label_size``, or the
#: values' own ``value_label_size``) an upright value would touch
#: another value or another bar. Every value is on the figure, so it is
#: advisory: a dashboard card reads its values upright, and the remedy is
#: ``value_label_fit`` stagger on a bar, stacked bar or combo chart, a
#: smaller ``label_size`` - or ``value_label_size`` when the chart sets one -
#: or more width.
#:
#: ``shrunk``: a ``pie`` or ``donut`` whose values are set under the
#: size its ``label_size`` asks - at that size the ring they take outside the
#: disc would leave it under the radius it keeps, or two of them would touch -
#: or, with no room outside even at the smallest size, written inside the
#: slices large enough to hold them. Every value it writes is on the figure,
#: so it is advisory; a value with no room anywhere is a ``values`` finding.
#:
#: ``size``: a block that sets its size - a ``metrics_grid`` - keeps
#: it when its body is shrunk to fit (``fit.ts`` holds it), so a KPI row reads
#: the same on every slide. Only a body that would still overflow at the floor
#: lets go of it; the block is then drawn below its size, at the body's ratio,
#: and no longer matches the same block on the other slides.
#:
#: The ``org_*`` findings read one ``org_chart`` observation: the layout
#: engine's own account of its picture and what the page painted.
#: ``org_scale`` - the chart would be drawn under the engine's minimum type
#: size, so the engine refused it and the slide shows a notice (overloaded);
#: ``org_width`` - a level is wider than the body, so the chart is scaled
#: down; ``org_label`` - a name or a role cut inside a word, or painted past
#: the width it was fitted to; ``org_siblings`` - one manager's cards at
#: different heights; ``org_crossing`` - a line through a seat it does not
#: join; ``org_bubble`` - a circle's label over another seat; ``org_counts`` -
#: a team's declared total that its reports do not add up to (all dense).
#: Advisory: ``org_crowded`` - more than ``ORG_CHART_READABLE_NODES`` seats
#: drawn with nothing folded; ``org_undated`` - a level of planned seats none
#: of which says when; ``org_hint`` - an ``architecture`` block drawn as a
#: reporting tree, read off the spec (``org_chart_hints``).
#:
#: ``unsettled``: an ``architecture`` figure whose room kept changing - its
#: drawing's ratio and the slide's fit fed back into each other - so the
#: renderer froze it at its last room rather than re-render it forever
#: (#11580): dense, the figure named with the options that cause the loop.
#:
#: ``zone_chips``: an ``architecture`` zone whose row of chips does not
#: fit its head row - it meets the logo / label or runs past the zone - even a
#: size down. The row is never wrapped or cut, so it is drawn over what it
#: meets: dense, the zone named as a validation refusal names it.
#:
#: ``arch_row``: an ``architecture`` grid row whose cards differ in height by
#: more than ``ARCH_ROW_SPREAD_PX`` (advisory): the row reads uneven.
#:
#: ``uneven`` and ``floating`` read the ``sparse`` observation's balance
#: facts (the columns' bottom edges, the count of blocks drawn): columns that
#: end more than ``UNEVEN_ABOVE`` of the body apart, and a lone top-aligned
#: block over more than ``FLOATING_ABOVE`` of bare body (both advisory).
#: ``top_heavy`` reads the same observation: a body of several blocks ending
#: with more than ``TOP_HEAVY_ABOVE`` of it bare, under ``SPARSE_ABOVE``.
#:
#: ``wrapped``: a card title or a name (``wrapped`` observation, its line
#: count) drawn on more than ``CARD_TITLE_MAX_LINES`` (advisory).
#:
#: ``unfilled``: a body that asks to fill its room (stretch, distribute,
#: fill) ending more than ``UNFILLED_ABOVE`` of it above the floor, or a
#: ``fill`` table or a map in a dashboard panel short of the panel by as much
#: (``panel_fill`` observation); graded ``dense``.
#:
#: ``wrap_title``: a slide, panel or chart title drawn on more than
#: ``ONE_LINE_MAX_LINES``; ``wrap_legend``: a chart legend (not one placed
#: ``right``, a column by design) on more rows than that (both advisory).
FindingKind = Literal[
    "fit",
    "clipped",
    "nowrap",
    "overflow",
    "sparse",
    "logo",
    "lines",
    "columns",
    "chart",
    "values",
    "rotated",
    "shrunk",
    "size",
    "org_scale",
    "org_width",
    "org_label",
    "org_siblings",
    "org_crossing",
    "org_bubble",
    "org_counts",
    "org_crowded",
    "org_undated",
    "org_hint",
    "unsettled",
    "map_overlap",
    "zone_chips",
    "arch_row",
    "uneven",
    "floating",
    "top_heavy",
    "wrapped",
    "wrap_title",
    "wrap_legend",
    "unfilled",
]
#: ``dense`` is a slide the audience reads with difficulty, ``overloaded`` one
#: they cannot; ``advisory`` is a slide that reads fine and looks unfinished
#: (``sparse``, logos lost in their box) - a hint, never a failure.
Severity = Literal["dense", "overloaded", "advisory"]

_SEVERITY_ORDER: dict[str, int] = {"overloaded": 0, "dense": 1, "advisory": 2}
#: The findings that measure EXCESS. A slide carrying one of them is never also
#: reported sparse: the two remedies contradict each other.
_EXCESS_KINDS: frozenset[str] = frozenset(
    {"fit", "clipped", "nowrap", "overflow", "lines", "columns", "chart", "size", "org_scale", "org_width"}
)
#: The findings that measure a DEFICIT of the body: silenced together by an
#: excess finding on the same slide.
_DEFICIT_KINDS: frozenset[str] = frozenset({"sparse", "uneven", "floating", "top_heavy", "unfilled"})


@dataclass(frozen=True, slots=True)
class LayoutFinding:
    """One thing the audience would see go wrong on one slide."""

    slide_index: int
    slide_id: str | None
    layout: str | None
    title: str
    kind: FindingKind
    severity: Severity
    message: str
    scale: float | None = None
    amount: int | None = None

    def as_dict(self) -> dict:
        return {
            "slide_index": self.slide_index,
            "slide_number": self.slide_index + 1,
            "slide_id": self.slide_id,
            "layout": self.layout,
            "title": self.title,
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "scale": self.scale,
            "amount": self.amount,
        }


@dataclass(frozen=True, slots=True)
class LayoutReport:
    """The outcome of one check: findings, or the reason there are none."""

    slide_count: int
    findings: tuple[LayoutFinding, ...] = ()
    #: Set when the check did not run to completion (disabled, timed out, no
    #: browser, the deck changed under the render); the findings are then
    #: meaningless and the note says why.
    skipped: str | None = None
    #: ``"slide"`` or ``"page"`` - the noun every line uses, from the deck's kind.
    unit: str = "slide"
    #: The revision the render measured, when the page published one.
    revision: int | None = None

    @property
    def clean(self) -> bool:
        return self.skipped is None and not self.findings

    def as_dict(self) -> dict:
        return {
            "slide_count": self.slide_count,
            "skipped": self.skipped,
            "unit": self.unit,
            "revision": self.revision,
            "findings": [finding.as_dict() for finding in self.findings],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> LayoutReport:
        """Inverse of :meth:`as_dict` (the cache round-trip)."""
        finding_fields = {f.name for f in fields(LayoutFinding)}
        findings = tuple(
            LayoutFinding(**{k: v for k, v in item.items() if k in finding_fields})
            for item in payload.get("findings") or []
            if isinstance(item, dict)
        )
        return cls(
            slide_count=int(payload.get("slide_count") or 0),
            findings=findings,
            skipped=payload.get("skipped") or None,
            unit=str(payload.get("unit") or "slide"),
            revision=payload.get("revision"),
        )


def layout_check_enabled() -> bool:
    return bool(settings.presentation_layout_check_enabled)


# ── Observations -> findings ─────────────────────────────────────────────────


def _slides(spec: dict) -> list:
    slides = spec.get("slides") if isinstance(spec, dict) else None
    return slides if isinstance(slides, list) else []


def _slide_spec(spec: dict, index: int) -> dict:
    slides = _slides(spec)
    if 0 <= index < len(slides) and isinstance(slides[index], dict):
        return slides[index]
    return {}


def _slide_title(spec: dict, index: int) -> str:
    title = _slide_spec(spec, index).get("title")
    return title.strip() if isinstance(title, str) and title.strip() else ""


def _join_remedies(remedies: list[str], lead: str = "") -> str:
    """``a, b, or c`` - one sentence, the first word capitalised (or *lead* before it)."""
    if len(remedies) > 1:
        remedies = [*remedies[:-1], f"or {remedies[-1]}"]
    sentence = (", " if len(remedies) > 2 else " ").join(remedies)
    if lead:
        return lead[0].upper() + lead[1:] + sentence + "."
    return sentence[0].upper() + sentence[1:] + "."


def sparse_remedy(spec: dict, index: int, unit: str) -> str:
    """The remedies a sparse content slide actually HAS, read off its spec.

    Blocks are sized to their content: the remedies never stretch a
    card or a grid (no ``fill``, no ``distribute``). They are: add content to
    the body, add a ``footer_block`` takeaway (a slide that holds one is never
    sparse, see :func:`_finding_from_observation`), and a merge with the neighbour that exists.
    """
    remedies: list[str] = ["add content to the body"]
    if not isinstance(_slide_spec(spec, index).get("footer_block"), dict):
        remedies.append("add a footer_block takeaway")
    if index + 1 < len(_slides(spec)):
        remedies.append(f"merge with the next {unit}")
    elif index > 0:
        remedies.append(f"merge with the previous {unit}")
    return _join_remedies(remedies)


def _balance_findings(observation: dict, *, spec: dict, common: dict, unit: str) -> list[LayoutFinding]:
    """The balance facts of a ``sparse`` observation, graded: ``uneven`` and ``floating``.

    Both are advisory and their remedies are a placement or content read off
    the slide, never a stretch of one card to the floor.
    """
    index = int(common["slide_index"])
    slide_spec = _slide_spec(spec, index)
    room = _number(observation.get("room"))
    if not room or room <= 0:
        return []
    findings: list[LayoutFinding] = []
    raw_ends = observation.get("ends")
    # A column with no block drawn reports null: it is no bottom edge (``_number`` reads null as 0).
    ends = [
        (n, None if e is None else _number(e))
        for n, e in enumerate(raw_ends if isinstance(raw_ends, list) else [], start=1)
    ]
    drawn = [(n, e) for n, e in ends if e is not None]
    if len(drawn) >= 2:
        (short, low), (_tall, high) = min(drawn, key=lambda c: c[1]), max(drawn, key=lambda c: c[1])
        apart = (high - low) / room
        if apart > UNEVEN_ABOVE:
            columns = slide_spec.get("columns")
            single = isinstance(columns, list) and columns and all(isinstance(c, list) and len(c) == 1 for c in columns)
            remedies = [f"add a block to column {short} (a metric, a list or a takeaway card)"]
            if single and not slide_spec.get("equal_height") and slide_spec.get("vertical_align") != "stretch":
                remedies.append("set equal_height: true so the cards share the tallest one's height")
            remedies.append("move a block from the longer column to it")
            findings.append(
                LayoutFinding(
                    **common,
                    kind="uneven",
                    severity="advisory",
                    amount=round(apart * 100),
                    message=(
                        f"the columns end {round(apart * 100)}% of the body apart - column {short} stops "
                        f"{round(high - low)}px above the longest one. {_join_remedies(remedies)}"
                    ),
                )
            )
    blocks = _number(observation.get("blocks"))
    unused = _number(observation.get("unused"))
    if unused is None or not 0 < unused <= 1:
        return findings
    empty = _number(observation.get("empty"))
    slide_height = _number(observation.get("slide"))
    share = empty / slide_height if empty and slide_height else unused
    percent = max(1, round(share * 100))
    if blocks == 1 and not drawn and unused > FLOATING_ABOVE:
        findings.append(
            LayoutFinding(
                **common,
                kind="floating",
                severity="advisory",
                amount=percent,
                message=(
                    f"the body is one block packed under the title, {percent}% of the {unit} bare below it. "
                    f"{_placement_remedy(slide_spec)}, or add the metric, list or visual that carries it."
                ),
            )
        )
    if unused > UNFILLED_ABOVE and _asks_fill(slide_spec):
        # The slide asked the body to take its room and its content is too
        # short to reach the floor (the spread is bounded by design): graded
        # above every placement hint, which would all name a remedy the
        # slide already chose.
        findings.append(
            LayoutFinding(
                **common,
                kind="unfilled",
                severity="dense",
                amount=percent,
                message=(
                    f"the body asks to fill its room ({_fill_request(slide_spec)}) and ends {percent}% of the "
                    f"{unit} above the floor: blocks keep their content's height and a fill spread is "
                    "bounded, so content this short cannot reach the floor. Set vertical_align: center "
                    "instead, or add the block that carries the claim."
                ),
            )
        )
    elif blocks and blocks >= 2 and not drawn and TOP_HEAVY_ABOVE < unused <= SPARSE_ABOVE:
        # Under the sparse threshold, over a third: the body sits in the top
        # two thirds and the bottom third reads unfinished.
        findings.append(
            LayoutFinding(
                **common,
                kind="top_heavy",
                severity="advisory",
                amount=percent,
                message=(
                    f"the body sits in the top of the {unit}, {percent}% of it bare below the last block. "
                    f"{_placement_remedy(slide_spec)}, or add the block that carries the claim."
                ),
            )
        )
    return findings


def _fill_blocks(slide_spec: dict) -> list[dict]:
    """The body's blocks, single-flow or in columns, that are dicts."""
    blocks: list = []
    if isinstance(slide_spec.get("blocks"), list):
        blocks.extend(slide_spec["blocks"])
    for column in slide_spec.get("columns") or []:
        if isinstance(column, list):
            blocks.extend(column)
    return [b for b in blocks if isinstance(b, dict)]


def _fill_request(slide_spec: dict) -> str:
    """What the slide asked of its body's height, in the spec's words; empty when nothing."""
    asked = []
    if slide_spec.get("vertical_align") == "stretch":
        asked.append("vertical_align: stretch")
    if slide_spec.get("distribute") is True:
        asked.append("distribute: true")
    if any(b.get("fill") is True for b in _fill_blocks(slide_spec)):
        asked.append("fill: true")
    return ", ".join(asked)


def _asks_fill(slide_spec: dict) -> bool:
    """True when the slide asks its body to take the room: stretch, distribute or a ``fill`` block."""
    return bool(_fill_request(slide_spec))


#: What a ``panel_fill`` observation's block is called in the sentence.
_PANEL_FILL_LABELS: dict[str, str] = {"table": "fill table", "map": "map"}


def _panel_fill_finding(observation: dict, *, common: dict, unit: str) -> LayoutFinding | None:
    """A ``fill`` table or a map in a dashboard panel that leaves the panel's figure row part empty."""
    unused = _number(observation.get("unused"))
    if unused is None or not UNFILLED_ABOVE < unused <= 1:
        return None
    block = _PANEL_FILL_LABELS.get(str(observation.get("block") or ""), "figure")
    panel = int(_number(observation.get("panel")) or 0)
    where = f"panel {panel}" if panel > 0 else "a panel"
    percent = max(1, round(unused * 100))
    remedy = (
        "widen the panel (span) or pick the region that matches its shape"
        if block == "map"
        else "report it as a renderer defect, or drop fill: true so the table is sized to its rows"
    )
    return LayoutFinding(
        **common,
        kind="unfilled",
        severity="dense",
        amount=percent,
        message=(
            f"the {block} in {where} leaves {percent}% of the panel empty on this {unit}. "
            f"{remedy[0].upper()}{remedy[1:]}."
        ),
    )


#: The label a ``wrapped`` observation's block is named by in the sentence.
_WRAPPED_LABELS: dict[str, str] = {
    "card-title": "card title",
    "feature-title": "feature title",
    "persona-name": "persona name",
    "tier-name": "tier name",
    "profile-name": "profile name",
}


def _wrapped_finding(observation: dict, *, common: dict) -> LayoutFinding | None:
    """A card title or a name drawn on more than ``CARD_TITLE_MAX_LINES``."""
    lines = _number(observation.get("lines"))
    if lines is None or lines <= CARD_TITLE_MAX_LINES:
        return None
    count = round(lines)
    label = _WRAPPED_LABELS.get(str(observation.get("block") or ""), "card title")
    return LayoutFinding(
        **common,
        kind="wrapped",
        severity="advisory",
        amount=count,
        message=(
            f"the {label}{_quote(observation.get('text'))} wraps to {count} lines - a title reads on one. "
            "Shorten it to its key words, or use fewer columns so each card is wider."
        ),
    )


#: How a ``wrap_title`` observation's owner is named, and what widens it.
_WRAP_TITLE_REMEDIES: dict[str, str] = {
    "slide": "Shorten it to the claim in its key words; a second idea is a second slide.",
    "panel": (
        "Shorten it: drop a qualifier the slide title already carries (the period, the unit, the scope), "
        "or widen the panel (a larger span, or fewer panel_columns)."
    ),
    "chart": (
        "Shorten it: drop a qualifier the slide title already carries (the period, the unit, the scope), "
        "or give the chart a wider column."
    ),
}


def _wrap_place(observation: dict) -> str:
    """`` in panel 2``, `` in chart 1``, or nothing: where the label sits."""
    panel = round(_number(observation.get("panel")) or 0)
    if panel > 0:
        return f" in panel {panel}"
    chart = round(_number(observation.get("chart")) or 0)
    return f" in chart {chart}" if chart > 0 else ""


def _wrap_title_finding(observation: dict, *, common: dict) -> LayoutFinding | None:
    """A slide, panel or chart title drawn on more than ``ONE_LINE_MAX_LINES``."""
    lines = _number(observation.get("lines"))
    if lines is None or lines <= ONE_LINE_MAX_LINES:
        return None
    count = round(lines)
    owner = str(observation.get("owner") or "")
    if owner not in _WRAP_TITLE_REMEDIES:
        owner = "slide"
    place = "" if owner == "slide" else _wrap_place(observation)
    return LayoutFinding(
        **common,
        kind="wrap_title",
        severity="advisory",
        amount=count,
        message=(
            f"the {owner} title{place}{_quote(observation.get('text'))} wraps to {count} lines - a title reads "
            f"on one. {_WRAP_TITLE_REMEDIES[owner]}"
        ),
    )


def _wrap_legend_finding(observation: dict, *, common: dict) -> LayoutFinding | None:
    """A chart legend whose entries take more than ``ONE_LINE_MAX_LINES`` rows."""
    rows = _number(observation.get("rows"))
    if rows is None or rows <= ONE_LINE_MAX_LINES:
        return None
    count = round(rows)
    series = round(_number(observation.get("series")) or 0)
    entries = f"{series} entries" if series > 0 else "entries"
    in_panel = round(_number(observation.get("panel")) or 0) > 0
    widen = "widen the panel (a larger span, or fewer panel_columns)" if in_panel else "give the chart a wider column"
    beside = "" if in_panel else ", or set legend_position: right"
    return LayoutFinding(
        **common,
        kind="wrap_legend",
        severity="advisory",
        amount=count,
        message=(
            f"the chart legend{_wrap_place(observation)}{_quote(observation.get('text'))} wraps its {entries} "
            f"onto {count} rows - a legend reads on one. Shorten the series names, merge the smallest series "
            f"into one, or {widen}{beside}."
        ),
    )


def _placement_remedy(slide_spec: dict) -> str:
    """The placement that moves a top-packed body, read off the slide.

    ``vertical_align: center`` moves a run sized to its content; a ``fill``
    block takes the room (its rows spread to a capped gap) and is never moved
    by it, so its remedy is to drop ``fill`` and let the body be centred.
    """
    blocks = slide_spec.get("blocks")
    fill = isinstance(blocks, list) and any(isinstance(b, dict) and b.get("fill") is True for b in blocks)
    if fill:
        return "Drop fill: true so the block is sized to its rows and centred"
    return "Set vertical_align: center"


def _quote(text: object) -> str:
    excerpt = " ".join(str(text or "").split())
    if len(excerpt) > 60:
        excerpt = excerpt[:57].rstrip() + "..."
    return f' "{excerpt}"' if excerpt else ""


def _block_label(block: object) -> str:
    label = str(block or "").strip()
    return label.replace("-", " ") if label else "element"


#: The size a too-long statement is pointed at: one step down the quote scale
#: (``QUOTE_SIZES``, smallest first); ``md`` is the floor, so its remedy names none.
_SMALLER_QUOTE_SIZE: dict[str, str] = dict(zip(QUOTE_SIZES[1:], QUOTE_SIZES, strict=False))
#: The same for a block drawn below its ``size``, on the block scale.
_BLOCK_SIZES: tuple[str, ...] = typing.get_args(Size)
_SMALLER_BLOCK_SIZE: dict[str, str] = dict(zip(_BLOCK_SIZES[1:], _BLOCK_SIZES, strict=False))


def _number(value: object) -> float | None:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return None


def _finding_from_observation(observation: dict, *, slide: dict, spec: dict, unit: str) -> LayoutFinding | None:
    index = int(slide.get("index") or 0)
    common = {
        "slide_index": index,
        "slide_id": slide.get("slide_id") or None,
        "layout": slide.get("layout") or None,
        "title": _slide_title(spec, index),
    }
    kind = observation.get("kind")
    if kind == "fit":
        scale = _number(observation.get("scale"))
        if scale is None or not 0 < scale < FIT_DENSE_BELOW:
            return None
        excess = round((1 / scale - 1) * 100)
        severity: Severity = "overloaded" if scale < FIT_OVERLOADED_BELOW else "dense"
        if scale <= FIT_FLOOR:
            # At the floor the renderer stops scaling and clips instead: the
            # excess is a lower bound, and the cut is what the audience loses.
            cut = round(_number(observation.get("cut")) or 0)
            lost = f" ({cut}px)" if cut > 0 else ""
            shrunk = (
                f"the body needed at least {excess}% more height than the {unit} has and was shrunk to "
                f"{round(scale * 100)}%, the floor - what still does not fit{lost} is cut off at the bottom"
            )
        else:
            shrunk = (
                f"the body needed {excess}% more height than the {unit} has and was shrunk to "
                f"{round(scale * 100)}% so nothing overlaps - the text is too small to read from the room"
            )
        return LayoutFinding(
            **common,
            kind="fit",
            severity=severity,
            scale=round(scale, 3),
            amount=excess,
            message=(
                f"{shrunk}. Cut content: fewer or shorter list items, one card or column less, "
                f"or move part of it to a new {unit}."
            ),
        )
    if kind == "sparse":
        unused = _number(observation.get("unused"))
        if unused is None or not SPARSE_ABOVE < unused <= 1:
            return None
        # A footer_block closes a slide that ends early on purpose.
        if isinstance(_slide_spec(spec, index).get("footer_block"), dict):
            return None
        # The threshold is a share of the BODY (what the agent can fill); the
        # sentence states the share of the SLIDE, which is what the room sees.
        empty = _number(observation.get("empty"))
        slide_height = _number(observation.get("slide"))
        share = empty / slide_height if empty and slide_height else unused
        percent = max(1, round(share * 100))
        return LayoutFinding(
            **common,
            kind="sparse",
            severity="advisory",
            amount=percent,
            message=(
                f"the body leaves {percent}% of the {unit} empty below the last block. "
                f"{sparse_remedy(spec, index, unit)}"
            ),
        )
    if kind == "logo":
        return _logo_finding(observation, common=common, unit=unit)
    if kind == "chart":
        return _chart_finding(observation, common=common, unit=unit)
    if kind == "values":
        return _values_finding(observation, common=common)
    if kind == "rotated":
        return _rotated_finding(observation, common=common)
    if kind == "shrunk":
        return _shrunk_finding(observation, common=common)
    if kind == "size":
        return _size_finding(observation, common=common, unit=unit)
    if kind == "unsettled":
        return _unsettled_finding(observation, common=common)
    if kind == "map_overlap":
        return _map_overlap_finding(observation, common=common)
    if kind == "zone_chips":
        return _zone_chips_finding(observation, common=common)
    if kind == "arch_row":
        return _arch_row_finding(observation, common=common)
    if kind == "columns":
        count = round(_number(observation.get("count")) or 0)
        per_row = round(_number(observation.get("per_row")) or 0)
        need = round(_number(observation.get("need")) or 0)
        room = round(_number(observation.get("room")) or 0)
        if count < 2 or not 0 < per_row < count:
            return None
        return LayoutFinding(
            **common,
            kind="columns",
            severity="dense",
            amount=need,
            message=(
                f"the columns block cannot keep its {count} columns side by side - its widest column needs "
                f"{need}px and each column gets {room}px - so they wrap onto rows of {per_row}. Put less side "
                "by side in the widest column (fewer logos on a row, a narrower table, shorter unbreakable words), "
                "or use fewer columns."
            ),
        )
    if kind == "wrapped":
        return _wrapped_finding(observation, common=common)
    if kind == "wrap_title":
        return _wrap_title_finding(observation, common=common)
    if kind == "wrap_legend":
        return _wrap_legend_finding(observation, common=common)
    if kind == "lines":
        lines = _number(observation.get("lines"))
        if lines is None or lines <= STATEMENT_MAX_LINES:
            return None
        count = round(lines)
        size = str(observation.get("size") or "").strip()
        remedies = ["cut it to its strongest sentence"]
        smaller = _SMALLER_QUOTE_SIZE.get(size)
        if smaller:
            remedies.append(f"set size {smaller}")
        remedies.append("make it a pull quote beside the context")
        # A hero past three lines is drawn one step down (`fitHeroQuote`): say
        # the size its lines were counted at, not the one the spec names.
        drawn = (
            " at the h1 size a hero steps down to"
            if size == "hero" and observation.get("stepped") is True
            else f" at size {size}" if size else ""
        )
        return LayoutFinding(
            **common,
            kind="lines",
            severity="dense",
            amount=count,
            message=(
                f"the statement quote{_quote(observation.get('text'))} runs to {count} lines"
                f"{drawn} - a statement is read in one breath, in at most "
                f"{STATEMENT_MAX_LINES}. {_join_remedies(remedies)}"
            ),
        )
    amount = round(_number(observation.get("amount")) or 0)
    block = _block_label(observation.get("block"))
    quoted = _quote(observation.get("text"))
    side = str(observation.get("side") or "bottom")
    severity = "overloaded" if amount >= CUT_OVERLOADED_PX else "dense"
    if kind == "clipped":
        # The footer / header bars are the deck settings rendered, not slide
        # content: the remedy is a shorter event label or field value,
        # never an edit of the slide the finding is reported on.
        if block in ("chrome footer", "chrome header"):
            what = "the footer text" if block == "chrome footer" else "the header text"
            remedy = (
                "Shorten what fills it - the deck's event label, footer text or template "
                "variables (chrome.fields): it is a deck setting drawn on every "
                f"{unit}, not this {unit}'s content."
            )
        elif block == "profile line":
            # A `profiles` line is ONE line by design: it already shrank
            # a type step before the ellipsis, so what is left is the text. The
            # cut is horizontal: what widens the row is a wider card (fewer
            # columns) or a smaller type step (density: tight) - the portrait
            # sits above the lines and changes nothing about their width.
            what = f"the profile line{quoted}"
            remedy = (
                "Shorten that line (profiles.items[].lines[].text is one line, never wrapped), "
                "use fewer columns so each card is wider, or density: tight."
            )
        elif block in ("profile name", "profile title"):
            # The name and the role sit beside the portrait on ONE line each:
            # they already shrank a type step, so the remedy is the text, a
            # wider card (fewer columns) or a smaller portrait (photo_size: sm)
            # that leaves the identity more room.
            field = "name" if block == "profile name" else "title"
            what = f"the profile {field}{quoted}"
            remedy = (
                f"Shorten it (profiles.items[].{field} is one line, never wrapped), use fewer "
                "columns so each card is wider, or photo_size: sm."
            )
        else:
            what = f"the {block}{quoted}"
            remedy = f"Shorten the text or reduce what shares the {unit} with it."
        return LayoutFinding(
            **common,
            kind="clipped",
            severity=severity,
            amount=amount,
            message=f"{what} is cut off at its {side} edge by {amount}px - part of it is not visible. {remedy}",
        )
    if kind == "nowrap":
        return LayoutFinding(
            **common,
            kind="nowrap",
            severity=severity,
            amount=amount,
            message=(
                f"the {block}{quoted} does not wrap and runs {amount}px past its box, over what sits "
                "next to it. Shorten the label or use fewer of them on the line."
            ),
        )
    if kind == "overflow":
        return LayoutFinding(
            **common,
            kind="overflow",
            severity=severity,
            amount=amount,
            message=(
                f"the {block}{quoted} is painted {amount}px past the {side} edge of the {unit} and is "
                "cut off. Shorten it or cut what pushes it there."
            ),
        )
    return None


def _logo_finding(observation: dict, *, common: dict, unit: str) -> LayoutFinding | None:
    """A logo block whose smallest mark is unreadable, or whose marks are lost in their box.

    One finding per block, the unreadable mark first: its remedy (a taller
    logo, fewer per row) also fills the box.
    """
    block = _block_label(observation.get("block"))
    wall = block == "logo wall"
    per_row = "per_row" if wall else "columns"
    height = _number(observation.get("height"))
    coverage = _number(observation.get("coverage"))
    step = _number(observation.get("step"))
    if "contrast" in observation:
        # The faintest logo of the slide against the surface under it.
        contrast = _number(observation.get("contrast")) or 0
        if not 0 < contrast < LOGO_MIN_CONTRAST:
            return None
        return LayoutFinding(
            **common,
            kind="logo",
            severity="dense",
            amount=round(contrast * 100),
            message=(
                f"the {block} paints a logo{_quote(observation.get('text'))} at {contrast:.2f}:1 contrast "
                f"against the surface under it (under {LOGO_MIN_CONTRAST}:1), so the audience does not see it. "
                "Use the variant of the logo made for this background (a dark mark on a light slide, a light "
                "one on a dark slide), the template's own logo_mark (drawn in the slide's ink), or a framed "
                "logo (logo_border)."
            ),
        )
    if block == "architecture":
        # A node logo sized by logo_height that its column could not draw at
        # the step: a wide wordmark capped so the labels stay readable. Short
        # once the rounded height named is under the step (no wider margin).
        if height is None or step is None or not 0 < height < step - 0.5:
            return None
        return LayoutFinding(
            **common,
            kind="logo",
            severity="advisory",
            amount=round(height),
            message=(
                f"the architecture draws the logo of node{_quote(observation.get('text'))} {round(height)}px "
                f"high, under its logo_height step ({round(step)}px): the mark is too wide for its column. "
                "Lower logo_height on the row, put fewer nodes on the row, or use a squarer mark."
            ),
        )
    if height is not None and 0 < height < LOGO_MIN_HEIGHT_PX:
        return LayoutFinding(
            **common,
            kind="logo",
            severity="dense",
            amount=round(height),
            message=(
                f"the {block} draws its smallest logo{_quote(observation.get('text'))} {round(height)}px high "
                f"(under {LOGO_MIN_HEIGHT_PX}px) - too small to read from the room. Raise logo_height, put fewer "
                f"logos on a row ({per_row}), or give the {block} more of the {unit}'s width."
            ),
        )
    if coverage is not None and 0 < coverage < LOGO_MIN_COVERAGE:
        # Floored, so a share just under the bar never reads as the bar itself.
        percent = max(1, math.floor(coverage * 100))
        if wall:
            remedy = "Raise logo_height, or lower per_row so each card's rows fill it."
        elif observation.get("room") == "callout":
            # A packed grid ending a `fill` callout: the card is the box.
            remedy = (
                "Raise logo_height, set fill on the logo grid so its rows spread over the card, "
                "or drop fill on the callout."
            )
        else:
            remedy = "Raise logo_height, set rows so the rows fill the box, or drop fill."
        return LayoutFinding(
            **common,
            kind="logo",
            severity="advisory",
            amount=percent,
            message=(
                f"the logos of the {block} cover {percent}% of their box (under "
                f"{round(LOGO_MIN_COVERAGE * 100)}%) - logos too small for their space. {remedy}"
            ),
        )
    return None


def _chart_finding(observation: dict, *, common: dict, unit: str) -> LayoutFinding | None:
    """A chart painted under ``CHART_MIN_HEIGHT_PX`` - 0 included, the legend and caption over no plot."""
    height = _number(observation.get("height"))
    if height is None or height >= CHART_MIN_HEIGHT_PX:
        return None
    drawn = max(0, round(height))
    kind = str(observation.get("chart") or "").strip().replace("_", " ")
    what = f"the {kind} chart" if kind else "the chart"
    if drawn == 0:
        seen = "is drawn 0px high - its legend and caption show over no plot at all"
    else:
        seen = f"is drawn {drawn}px high (under {CHART_MIN_HEIGHT_PX}px) - too short to read"
    if observation.get("panel") is True:
        remedy = f"Give its panel more height: fewer key figures or panels on the {unit}, or split the {unit}."
    elif observation.get("fill") is True:
        remedy = (
            f"It takes the height its column or the {unit} leaves after the blocks around it: cut or shorten "
            f"those blocks, or move the chart to a {unit} of its own."
        )
    else:
        remedy = f"Cut what shares the {unit} with it, or move the chart to a {unit} of its own."
    return LayoutFinding(
        **common,
        kind="chart",
        severity="overloaded" if drawn < CHART_MIN_HEIGHT_PX / 2 else "dense",
        amount=drawn,
        message=f"{what}{_quote(observation.get('text'))} {seen}. {remedy}",
    )


def _size_finding(observation: dict, *, common: dict, unit: str) -> LayoutFinding | None:
    """A block that sets its size, drawn below it because its body would overflow at it."""
    scale = _number(observation.get("scale"))
    if scale is None or not 0 < scale < 1:
        return None
    block = str(observation.get("block") or "").strip() or "block"
    size = str(observation.get("size") or "").strip()
    percent = round(scale * 100)
    named = f"size {size}" if size else "its size"
    remedies = [f"cut what shares the {unit} with it", f"move part of that content to a new {unit}"]
    smaller = _SMALLER_BLOCK_SIZE.get(size)
    if smaller:
        remedies.append(f"set size {smaller} on every {block} of the deck")
    return LayoutFinding(
        **common,
        kind="size",
        severity="dense",
        scale=round(scale, 3),
        amount=percent,
        message=(
            f"the {block} is drawn at {percent}% of {named}: held at it, the rest of the {unit} would not fit "
            f"even at the smallest text the {unit} allows, so it shrank with the body and reads smaller than a "
            f"{block} of {named} anywhere else in the deck. {_join_remedies(remedies)}"
        ),
    )


def _unsettled_finding(observation: dict, *, common: dict) -> LayoutFinding:
    """An architecture figure or positioning map whose layout did not converge: frozen at its last room."""
    if observation.get("block") == "positioning_map":
        position = int(_number(observation.get("map")) or 1)
        return LayoutFinding(
            **common,
            kind="unsettled",
            severity="dense",
            amount=None,
            message=(
                f"positioning_map {position}: its plot's room kept changing, so it was frozen at its last size "
                "and its labels may sit off their marks. Give the map its own column or a full-width row "
                "(size full or fill), or take a block out of the slide so the body is not shrunk to fit."
            ),
        )
    position = int(_number(observation.get("architecture")) or 1)
    remedies = [
        "give the figure its own slide or a full-width row (size full or fill)",
        "drop logo_height on its nodes (or use one step for all)",
        "take a block out of the slide so the body is not shrunk to fit",
    ]
    return LayoutFinding(
        **common,
        kind="unsettled",
        severity="dense",
        amount=None,
        message=(
            f"architecture {position}: its layout did not converge - the drawing's height and the slide's "
            "shrink-to-fit kept resizing each other, so it was frozen at its last size and may be drawn "
            f"smaller or cut. {_join_remedies(remedies)}"
        ),
    )


def _map_overlap_finding(observation: dict, *, common: dict) -> LayoutFinding | None:
    """A positioning map whose labels (item names, quadrant labels, axis end labels) are drawn over another label or a mark."""
    amount = round(_number(observation.get("amount")) or 0)
    if amount <= 0:
        return None
    position = int(_number(observation.get("map")) or 1)
    labels = [" ".join(str(label).split()) for label in observation.get("labels") or [] if str(label).strip()]
    named = f" ({', '.join(repr(label) for label in labels)})" if labels else ""
    remedies = [
        "move the crowded items a few points apart on x or y",
        "shorten their labels or drop a sublabel",
        "give the map more room (size lg or fill, or its own slide)",
    ]
    return LayoutFinding(
        **common,
        kind="map_overlap",
        severity="dense",
        amount=amount,
        message=(
            f"positioning_map {position}: {amount} label(s){named} found no free side and are drawn over "
            f"another label or mark. {_join_remedies(remedies)}"
        ),
    )


def _zone_chips_finding(observation: dict, *, common: dict) -> LayoutFinding | None:
    """An architecture zone whose row of chips does not fit its head row, even a size down."""
    amount = round(_number(observation.get("amount")) or 0)
    if amount <= 0:
        return None
    index = int(_number(observation.get("zone")) or 0)
    position = int(_number(observation.get("architecture")) or 1)
    label = " ".join(str(observation.get("label") or "").split())
    zone = f"zones.{index} ({label!r})" if label else f"zones.{index}"
    remedies = ["shorten its chips or drop one", "widen the zone (col_span)"]
    if label:
        remedies.append("shorten its label")
    return LayoutFinding(
        **common,
        kind="zone_chips",
        severity="dense",
        amount=amount,
        message=(
            f"architecture {position}: the chips of {zone} need {amount}px more than its head row leaves them, "
            "even one size down, so they are drawn over its logo / label or past the zone - a zone's chips are "
            f"one row, never wrapped. {_join_remedies(remedies)}"
        ),
    )


#: The most two cards of one ``architecture`` row may differ in height (drawing units).
ARCH_ROW_SPREAD_PX = 2


def _arch_row_finding(observation: dict, *, common: dict) -> LayoutFinding | None:
    """An architecture grid row whose cards are not one height: the row reads uneven."""
    amount = _number(observation.get("amount")) or 0
    if amount <= ARCH_ROW_SPREAD_PX:
        return None
    row = int(_number(observation.get("row")) or 0)
    position = int(_number(observation.get("architecture")) or 1)
    unsized = int(_number(observation.get("unsized")) or 0)
    tiles = int(_number(observation.get("tiles")) or 0)
    if unsized and tiles:
        # A logo with no logo_height keeps its own box beside the icon tiles:
        # only a size step on those logos puts the row on one media height.
        logos = "card" if unsized == 1 else "cards"
        remedy = (
            f"Set logo_height on its {unsized} logo {logos} (any step), so the logos and the icon "
            "tiles share one media height."
        )
    else:
        remedy = (
            "Give its cards the same label and sublabel line counts and the same chips, "
            "or move the odd card to its own row."
        )
    return LayoutFinding(
        **common,
        kind="arch_row",
        severity="advisory",
        amount=round(amount),
        message=(
            f"architecture {position}: the cards of row {row} differ in height by {round(amount)}px, so the row "
            f"reads uneven. {remedy}"
        ),
    )


#: What a chart that could not write its values is told to change, by the
#: reason its drawing published (``HiddenValuesReason`` in ``chart-layout.ts``).
_VALUES_REMEDIES: dict[str, str] = {
    "narrow": (
        "Give the chart more width (a full-width row instead of a column, or a wider column), "
        "fewer categories or series, or set show_values false to leave the numbers to the tooltip."
    ),
    "short": (
        "Give the chart more height (size md or lg, or fill), or set show_values false to leave "
        "the numbers to the tooltip."
    ),
    "dense": "Group the smallest slices into one (Other), or set show_values false.",
    "region": (
        "Drop region to draw the whole world, or take these countries out of this map (a second map of their "
        "region, or a table)."
    ),
    # Worded from what the drawing found more room would do (``_segments_remedy``);
    # this is what an observation that says nothing about it is told.
    "segments": "Leave it: the stack totals are written and the tooltip carries every segment.",
    "overlap": (
        "Give the chart more height (size lg, or fill) so the line parts from the bars, put the line on "
        "the right-hand axis (axis secondary) and pin the two axes apart (y_max raises the bars' ceiling, "
        "secondary_y_min lowers the line's floor), set show_values false on one series to leave its values "
        "to the tooltip, or set show_values false."
    ),
    "upright": (
        "Set value_label_fit stagger to write every value at this size on two alternating rows, a smaller "
        "label_size, or give the chart more width (a full-width row instead of a column) or fewer categories "
        "or series."
    ),
}
#: The ``upright`` remedy of a chart whose values have a size of their own
#: (``value_label_size``): ``label_size`` no longer sizes them.
_UPRIGHT_REMEDY_OWN_VALUE_SIZE = (
    "Set value_label_fit stagger to write every value at this size on two alternating rows, a smaller "
    "value_label_size, or give the chart more width (a full-width row instead of a column) or fewer categories "
    "or series."
)

#: The same, for a horizontal bar or stacked bar chart: its rows share
#: the plot's height and its values run along them toward the right edge, so
#: the room across the rows is more height and the room past the bars more width.
_HORIZONTAL_VALUES_REMEDIES: dict[str, str] = {
    "narrow": (
        "Set value_label_fit stagger to write every value at this size, a value in the way of another moved along "
        "its row past it; or give the chart more height (size md or lg, or fill), fewer categories or series, or "
        "set show_values false to leave the numbers to the tooltip."
    ),
    "short": (
        "Give the chart more width (a full-width row instead of a column, or a wider column), or set show_values "
        "false to leave the numbers to the tooltip."
    ),
    "upright": (
        "Set value_label_fit stagger to write every value at this size, a value in the way of another moved along "
        "its row past it; a smaller label_size, or give the chart more height (size md or lg, or fill) or fewer "
        "categories or series."
    ),
}
#: Its ``upright`` remedy when the values have a size of their own (``value_label_size``).
_HORIZONTAL_UPRIGHT_REMEDY_OWN_VALUE_SIZE = _HORIZONTAL_VALUES_REMEDIES["upright"].replace(
    "a smaller label_size", "a smaller value_label_size"
)

#: The chart kinds drawn on their side when ``horizontal`` is set (``horizontal`` in ``chart-layout.ts``), as ``_chart_named`` spells them.
_HORIZONTAL_CHART_KINDS: frozenset[str] = frozenset({"bar", "stacked bar"})

#: The chart kinds drawn from a central spine (``MIRRORED_CHART_KINDS``), as ``_chart_named`` spells them.
_MIRRORED_CHART_KINDS: frozenset[str] = frozenset({"butterfly"})
#: What such a chart is told (``chart-butterfly.ts``): its rows share the
#: plot's height, and its values run out from the spine toward both edges, so
#: the room past its bars is width it shares with the spine's labels.
_BUTTERFLY_VALUES_REMEDIES: dict[str, str] = {
    "narrow": (
        "Give the chart more height (size md or lg, or fill), fewer categories, a smaller value_label_size, or set "
        "show_values false to leave the numbers to the tooltip."
    ),
    "short": (
        "Give the chart more width (a full-width row instead of a column, or a wider column), put the category "
        "labels on the left axis (spine left) to give the bars the spine's column, or set show_values false to "
        "leave the numbers to the tooltip."
    ),
}


#: The chart kinds whose values sit on points (``pointsOnly`` in ``chart-layout.ts``), as ``_chart_named`` spells them.
_POINT_CHART_KINDS: frozenset[str] = frozenset({"line", "area"})
#: The chart kinds whose values sit on slices (``chart-slices.ts``), as ``_chart_named`` spells them.
_SLICE_CHART_KINDS: frozenset[str] = frozenset({"pie", "donut"})


def _slice_remedies(kind: str, observation: dict) -> list[str]:
    """The room a pie or a donut short of it for its values can be given:
    more width - its legend under it first, where it stands beside it."""
    remedies = ["give the chart more width (a full-width row instead of a column, or a wider column)"]
    if observation.get("legend_position") == "right":
        remedies.insert(0, f"move the legend under the chart (legend_position bottom) to give the {kind} its width")
    return remedies


def _slice_values_detail(observation: dict, kind: str, of: str) -> str:
    """Why a pie or a donut leaves values out: outside, the room along
    the rim beside the smallest slice is taken by its neighbours' values;
    inside - where the values go when no size leaves them room outside - its
    slice holds its value only in a larger disc."""
    share = _number(observation.get("share")) or 0
    smallest = f"the smallest of them is {share * 100:.1f}% of the {kind}" if share > 0 else f"a slice of the {kind}"
    room = round(_number(observation.get("room")) or 0)
    need = round(_number(observation.get("need")) or 0)
    if observation.get("placement") == "inside":
        return (
            f"{of} - with no room outside the {kind} even at the smallest size, its values are written inside the "
            f"slices that hold them, and {smallest}: its value would need a disc of {need}px in radius where this one "
            f"is {room}px"
        )
    return (
        f"{of} - {smallest}, {room}px along the rim where its value would be written outside it, and the value needs "
        f"{need}px there even at the smallest size, between its neighbours' values"
    )


def _chart_named(observation: dict) -> tuple[int, str, str]:
    """How a values finding names its chart: its position on the slide, its kind and its shape."""
    position = round(_number(observation.get("chart")) or 0) or 1
    kind = str(observation.get("chart_kind") or "").strip().replace("_", " ") or "chart"
    series = round(_number(observation.get("series")) or 0)
    categories = round(_number(observation.get("categories")) or 0)
    shape = f", {series} series x {categories} categories" if series > 0 and categories > 0 else ""
    return position, kind, shape


def _quoted_list(names: list[str]) -> str:
    """``"A"``, ``"A" and "B"``, ``"A", "B" and "C"``."""
    quoted = [f'"{name}"' for name in names]
    return quoted[0] if len(quoted) == 1 else f"{', '.join(quoted[:-1])} and {quoted[-1]}"


def _segments_detail(observation: dict, of: str) -> str:
    """Why a stack's segments keep values hidden: no room inside, none
    past the stack - with the thinnest one's share, its length and its need.
    A horizontal stack fitted by ``value_label_fit`` kept the size
    asked: under ``upright`` the room past the stack was not tried."""
    fit = observation.get("fit")
    if fit == "upright":
        detail = (
            f"{of} - kept at the size asked (value_label_fit upright), segments of the stacks are too thin to hold "
            "their value"
        )
    elif fit == "stagger":
        detail = (
            f"{of} - kept at the size asked (value_label_fit stagger), segments of the stacks are too thin to hold "
            "their value and the room past their stack is taken"
        )
    else:
        detail = f"{of} - segments of the stacks are too thin to hold their value and the room past their stack is taken"
    share = _number(observation.get("share")) or 0
    room = round(_number(observation.get("room")) or 0)
    need = round(_number(observation.get("need")) or 0)
    if share > 0 and need > 0:
        percent = f"{share * 100:.1f}%" if share >= 0.001 else "under 0.1%"
        detail += (
            f": the thinnest is {percent} of the axis range, {room}px long, and its value needs {need}px inside it"
        )
    return detail


def _segments_remedy(observation: dict) -> str:
    """What a stack hiding values in thin segments can change.

    More height or width is named only where the drawing planned the same
    chart at the tallest canvas a slide gives it (``taller``) or across a full
    row (``wider``) and found it writes every value; otherwise the message says
    the room would not, and points at what does: the series' own
    ``show_values`` false, or leaving it. Values a stack kept ``upright``
    are first offered ``stagger``, which writes them past the stack."""
    taller = observation.get("taller") is True
    wider = observation.get("wider") is True
    horizontal = observation.get("horizontal") is True
    reach = round(_number(observation.get("reach")) or 0)
    need = round(_number(observation.get("need")) or 0)
    sentences: list[str] = []
    if observation.get("fit") == "upright":
        sentences.append("Set value_label_fit stagger to write them past their stack, with a leader, at this size.")
    # The dimension that lengthens a segment: height upright, width on its side.
    if not (wider if horizontal else taller):
        lead = f"More {'width' if horizontal else 'height'} would not write them"
        if need > 0:
            where = "across a full row" if horizontal else f"{CHART_TALLEST_CANVAS_PX}px high, the most a slide gives"
            lead += f": even {where}, the thinnest segment would be {reach}px long and its value needs {need}px"
        sentences.append(lead + ".")
    if taller:
        if observation.get("panel") is True:
            how = "Give its panel more height (fewer key figures or panel rows on the slide)"
        else:
            how = f"Give the chart more height (size lg, or fill with at least {CHART_TALLEST_CANVAS_PX}px)"
        sentences.append(f"{how}: every value is written at {CHART_TALLEST_CANVAS_PX}px.")
    if wider:
        sentences.append("Give the chart more width (a full-width row instead of a column): every value is written there.")
    names = [str(name) for name in observation.get("hidden_series") or [] if str(name).strip()]
    series = f"series {_quoted_list(names)}" if names else "the series of those segments"
    whose = "its" if len(names) == 1 else "their"
    options = (
        f"set show_values false on {series} to leave {whose} values to the tooltip, or leave it: "
        "the stack totals are written and the tooltip carries every segment."
    )
    sentences.append(f"Otherwise {options}" if taller or wider else options[0].upper() + options[1:])
    return " ".join(sentences)


def _values_finding(observation: dict, *, common: dict) -> LayoutFinding | None:
    """A chart that writes fewer of its values than its block asked for."""
    asked = round(_number(observation.get("asked")) or 0)
    written = round(_number(observation.get("written")) or 0)
    if asked <= 0 or not 0 <= written < asked:
        return None
    hidden = asked - written
    position, kind, shape = _chart_named(observation)
    reason = str(observation.get("reason") or "")
    room = round(_number(observation.get("room")) or 0)
    need = round(_number(observation.get("need")) or 0)
    of = f"{hidden} of {asked} values are not written"
    # A horizontal chart's rows share the plot's height, its values run along them.
    sideways = observation.get("horizontal") is True and kind in _HORIZONTAL_CHART_KINDS
    mirrored = kind in _MIRRORED_CHART_KINDS
    if reason == "narrow" and kind in _SLICE_CHART_KINDS:
        detail = _slice_values_detail(observation, kind, of)
    elif mirrored and reason == "narrow":
        detail = f"{of} - the rows stand {room}px apart and even the smallest value needs {need}px across them"
    elif mirrored and reason == "short":
        detail = (
            f"{of} - its two sides keep {room}px for their bars once the values past them take their room, and need "
            f"{need}px to stay readable"
        )
    elif sideways and reason == "narrow":
        detail = f"{of} - the rows stand {room}px apart and even the smallest value needs {need}px across them"
    elif sideways and reason == "short":
        detail = f"{of} - the plot is {room}px wide and needs {need}px to keep its bars readable with the values past them"
    elif sideways and reason == "upright":
        detail = (
            f"{of} - kept at the size asked (value_label_fit upright), a value needs {need}px across the rows and "
            f"they stand {room}px apart even once the bars gave way, so a value that would touch the one beside it "
            "is left out"
        )
    elif reason == "narrow" and kind in _POINT_CHART_KINDS:
        # A line's values are tried in one row and alternating over and under
        # the points, two points' room each, before they are hidden.
        detail = (
            f"{of} - the run between two points is {room}px and even the smallest label needs {need}px, "
            "in one row or written alternately above and below the points"
        )
    elif reason == "narrow":
        detail = (
            f"{of} - the room between two bars (or two points) is {room}px and even the smallest "
            f"label, turned on its side, needs {need}px"
        )
    elif reason == "short":
        detail = f"{of} - the plot is {room}px long and needs {need}px to keep its bars or points readable with the values past them"
    elif reason == "dense":
        detail = f"{of} - a {kind} writes its values on at most {room} slices and this one has {need}"
    elif reason == "segments":
        detail = _segments_detail(observation, of)
    elif reason == "overlap":
        detail = (
            f"{of} - a line's value finds a bar, a bar's value or another line's over and under its point, "
            "and no room left above them"
        )
    elif reason == "upright":
        # `value_label_fit` upright: kept at the size asked, on one row. A
        # group of upright bars keeps its room: only a value is in the way.
        detail = (
            f"{of} - kept upright at the size asked (value_label_fit upright), the widest needs {need}px "
            f"across and the bars stand {room}px apart, so a value that would touch the one beside it is left out"
        )
    elif reason == "region":
        # A map zoomed on a region leaves out the countries beyond it.
        countries = [str(code) for code in observation.get("countries") or [] if str(code).strip()]
        named = f" ({', '.join(countries)})" if countries else ""
        region = str(observation.get("region") or "its region")
        detail = f"{of} - {hidden} of its {asked} countries{named} lie outside region {region}"
    else:
        detail = of
    if reason == "segments":
        remedy = _segments_remedy(observation)
    elif reason == "narrow" and kind in _SLICE_CHART_KINDS:
        remedy = _join_remedies(
            [
                *_slice_remedies(kind, observation),
                "group the smallest slices into one (Other)",
                "set show_values false to leave the numbers to the tooltip",
            ]
        )
    elif mirrored and reason in _BUTTERFLY_VALUES_REMEDIES:
        remedy = _BUTTERFLY_VALUES_REMEDIES[reason]
    elif reason == "upright" and observation.get("value_label_size") is not None:
        remedy = _HORIZONTAL_UPRIGHT_REMEDY_OWN_VALUE_SIZE if sideways else _UPRIGHT_REMEDY_OWN_VALUE_SIZE
    elif sideways and reason in _HORIZONTAL_VALUES_REMEDIES:
        remedy = _HORIZONTAL_VALUES_REMEDIES[reason]
    else:
        remedy = _VALUES_REMEDIES.get(reason, _VALUES_REMEDIES["narrow"])
    return LayoutFinding(
        **common,
        kind="values",
        severity="advisory" if reason == "segments" else "dense",
        amount=hidden,
        message=f"values hidden on chart {position} ({kind}{shape}): {detail}. {remedy}",
    )


#: The ``label_size`` one step down from each (``Size`` in ``presentation_spec.py``).
_SMALLER_LABEL_SIZE: dict[str, str] = {"lg": "md", "md": "sm"}
#: The step between two sizes of a value ladder (``VALUE_LADDER_STEP`` in ``chart-layout.ts``).
_VALUE_LADDER_STEP_PX = 2
#: The chart kinds ``value_label_fit`` acts on (``VALUE_LABEL_FIT_KINDS`` in ``chart-format.ts``), as ``_chart_named`` spells them.
_VALUE_LABEL_FIT_KINDS: frozenset[str] = frozenset({"bar", "stacked bar", "combo"})


def _smaller_value_label_size(value: object, upright: int) -> str | None:
    """The ``value_label_size`` that tries the values smaller than a chart
    whose own size turned them on their side, *upright* being the
    smallest it tried them upright in: a text scale one step down, or the
    number one step under - whose ladder reaches a step under *upright*. None
    once no size of the domain reaches lower."""
    if isinstance(value, str):
        return _SMALLER_LABEL_SIZE.get(value)
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if upright <= 0 or upright - _VALUE_LADDER_STEP_PX < VALUE_LABEL_SIZE_MIN_PX:
        return None
    return str(value - _VALUE_LADDER_STEP_PX)


def _rotated_finding(observation: dict, *, common: dict) -> LayoutFinding | None:
    """A chart whose values are turned on their side: all written, none upright."""
    position, kind, shape = _chart_named(observation)
    size = round(_number(observation.get("size")) or 0)
    upright = round(_number(observation.get("upright")) or 0)
    room = round(_number(observation.get("room")) or 0)
    need = round(_number(observation.get("need")) or 0)
    turned = f"the values are turned on their side{f' at {size}px' if size > 0 else ''}"
    at = f" at {upright}px" if upright > 0 else ""
    if room > 0 and need > room:
        why = f"set upright{at}, the widest needs {need}px across and the bars stand {room}px apart"
    else:
        why = f"set upright{at}, a value would touch the value or the bar beside it"
    remedies = []
    if kind in _VALUE_LABEL_FIT_KINDS:
        remedies.append("set value_label_fit stagger to keep them upright at this text size on two alternating rows")
    # Values with a size of their own do not follow label_size: their own is named.
    value_size = observation.get("value_label_size")
    if value_size is not None:
        smaller = _smaller_value_label_size(value_size, upright)
        if smaller:
            remedies.append(f"set value_label_size {smaller} for smaller values, the axis text unchanged")
    else:
        smaller = _SMALLER_LABEL_SIZE.get(str(observation.get("label_size") or "md"))
        if smaller:
            remedies.append(f"set label_size {smaller} for smaller values and axis text")
    remedies.append("give the chart more width (a full-width row instead of a column, or a wider column)")
    remedies.append("use fewer categories or series")
    return LayoutFinding(
        **common,
        kind="rotated",
        severity="advisory",
        amount=size or None,
        message=f"values rotated on chart {position} ({kind}{shape}): {turned} - {why}. {_join_remedies(remedies)}",
    )


def _shrunk_finding(observation: dict, *, common: dict) -> LayoutFinding | None:
    """A pie or a donut whose values are set under the size asked:
    every value it writes is on the figure, smaller than its ``label_size``
    sets it - or inside the slices, with no room outside at any size."""
    position, kind, shape = _chart_named(observation)
    size = round(_number(observation.get("size")) or 0)
    asked = round(_number(observation.get("asked_size")) or 0)
    if size <= 0 or not size < asked:
        return None
    label_size = str(observation.get("label_size") or "md")
    room = round(_number(observation.get("room")) or 0)
    need = round(_number(observation.get("need")) or 0)
    inside = observation.get("placement") == "inside"
    where = "inside the slices" if inside else f"outside the {kind}"
    shrunk = f"the values are set at {size}px {where}, under the {asked}px of label_size {label_size}"
    if room < need:
        why = (
            f"at {asked}px the values outside it would leave the {kind} a {room}px radius, "
            f"under the {need}px it keeps"
        )
    else:
        why = f"at {asked}px two values outside the {kind} would touch"
    if inside:
        why += ", and even the smallest size has no room outside it"
    remedies = _slice_remedies(kind, observation)
    smaller = _SMALLER_LABEL_SIZE.get(label_size)
    if smaller:
        remedies.append(f"set label_size {smaller} to make smaller values the chart's own")
    return LayoutFinding(
        **common,
        kind="shrunk",
        severity="advisory",
        amount=size,
        message=f"values shrunk on chart {position} ({kind}{shape}): {shrunk} - {why}. {_join_remedies(remedies)}",
    )


def _names(entries: object, part: int = 0, limit: int = 4) -> list[str]:
    """The distinct seat ids (or the other half of ``a|b``) of a list of ``a|b`` entries, the first *limit*."""
    out: list[str] = []
    for entry in entries if isinstance(entries, list) else []:
        pieces = str(entry).split("|")
        name = pieces[part] if part < len(pieces) else ""
        if name and name not in out:
            out.append(name)
    return out[:limit]


def _seats(names: list[str]) -> str:
    return ", ".join(repr(name) for name in names)


def _org_chart_findings(observation: dict, *, common: dict, unit: str) -> list[LayoutFinding]:
    """The findings of one ``org_chart`` observation: its engine's facts and what the page painted."""
    facts = observation.get("facts") if isinstance(observation.get("facts"), dict) else {}
    painted = observation.get("painted") if isinstance(observation.get("painted"), dict) else {}
    position = int(_number(observation.get("chart")) or 1)
    chart = f"org chart {position}"
    found: list[LayoutFinding] = []

    def finding(kind: FindingKind, severity: Severity, message: str, amount: int | None = None, scale: float | None = None):
        found.append(LayoutFinding(**common, kind=kind, severity=severity, message=message, amount=amount, scale=scale))

    drawn = int(_number(facts.get("drawn")) or 0)
    scale = _number(facts.get("scale"))
    if facts.get("refused"):
        min_type = _number(facts.get("minType")) or 0
        allowed = _number(facts.get("minAllowed")) or 0
        finding(
            "org_scale",
            "overloaded",
            f"{chart} draws {drawn} seats, which this room could only show at {min_type:g}px type, under the "
            f"{allowed:g}px minimum, so the {unit} shows a notice instead of the chart. Fold teams "
            "(collapse_below, depth_limit), draw the teams as bubbles (style hybrid or bubbles), give it the "
            f"{unit} (fill), or split it over two {unit}s.",
            amount=drawn,
            scale=scale,
        )
    wide = facts.get("wide") if isinstance(facts.get("wide"), dict) else None
    if wide and not facts.get("refused"):
        finding(
            "org_width",
            "dense",
            f"{chart}: level {int(_number(wide.get('level')) or 1)} is {int(_number(wide.get('width')) or 0)}px wide "
            f"and the room {int(_number(wide.get('room')) or 0)}px, so the chart is drawn at "
            f"{round((scale or 1) * 100)}% - fold the widest teams (collapse_below), stack their leaves (spine "
            "always), or draw them as bubbles.",
            scale=scale,
        )
    broken = list(dict.fromkeys([*_names(facts.get("broken"), 0), *_names(painted.get("overflow"), 0)]))
    if broken:
        finding(
            "org_label",
            "dense",
            f"{chart}: the name or role of {_seats(broken)} is cut inside a word or runs past its line - "
            "shorten it (a name, a title of a few words; the rest goes in the note), or set label_size sm.",
        )
    unequal = list(dict.fromkeys([*_names(facts.get("unequal")), *_names(painted.get("unequal"))]))
    if unequal:
        finding(
            "org_siblings",
            "dense",
            f"{chart}: the cards under {_seats(unequal)} are drawn at different heights - give their seats the "
            "same lines (a role and the same number of chips each).",
        )
    crossings = [*(facts.get("crossings") or []), *(painted.get("crossings") or [])]
    if crossings:
        through = _names(crossings, 1)
        finding(
            "org_crossing",
            "dense",
            f"{chart}: a line runs through {_seats(through)} - set show_dotted false, move the dotted_to "
            "line to a seat beside it, or set spine never.",
        )
    overlaps = [*(facts.get("overlaps") or []), *(painted.get("overlaps") or [])]
    if overlaps:
        finding(
            "org_bubble",
            "dense",
            f"{chart}: the label of {_seats(_names(overlaps, 0))} meets {_seats(_names(overlaps, 1))} - "
            "shorten the team's role, or draw it as a card (style cards).",
        )
    counts = [entry for entry in facts.get("counts") or [] if isinstance(entry, dict)]
    if counts:
        detail = "; ".join(
            f"{entry.get('id')!r} declares {entry.get('declared')} and its reports add up to {entry.get('computed')}"
            for entry in counts[:3]
        )
        finding(
            "org_counts",
            "dense",
            f"{chart}: the counts do not add up - {detail}. Set the team's count to its reports' total, or add "
            "the seats it leaves out (a bubble with the rest).",
            amount=len(counts),
        )
    if drawn > ORG_CHART_READABLE_NODES and not facts.get("folded") and not facts.get("refused"):
        finding(
            "org_crowded",
            "advisory",
            f"{chart} draws {drawn} seats and folds none - past {ORG_CHART_READABLE_NODES} a {unit} reads "
            "better with teams folded (collapse_below, depth_limit) or drawn as bubbles.",
            amount=drawn,
        )
    undated = [int(level) for level in facts.get("undated") or [] if isinstance(level, (int, float))]
    if undated:
        levels = ", ".join(str(level) for level in undated)
        finding(
            "org_undated",
            "advisory",
            f"{chart}: the planned seats of level {levels} say no period - give each the period it is hired in "
            '(period "H2 FY28").',
        )
    return found


def _org_hint_findings(spec: dict, index: int, common: dict) -> list[LayoutFinding]:
    """The architecture block of this slide drawn as a reporting tree, pointed at ``org_chart``."""
    return [
        LayoutFinding(**common, kind="org_hint", severity="advisory", message=text)
        for slide_index, text in org_chart_hints(spec)
        if slide_index == index
    ]


def findings_from_observations(observations: list[dict], spec: dict) -> list[LayoutFinding]:
    """Turn the raw per-slide observations of ``LAYOUT_AUDIT_JS`` into findings.

    Pure: no browser, no database. Sorted worst first (overloaded before dense
    before advisory, then deck order) so a truncated report still shows what
    matters most. The words follow the deck's kind: a page brief is told about
    its pages.
    """
    unit = unit_noun(spec, 1)
    findings: list[LayoutFinding] = []
    for slide in observations or []:
        if not isinstance(slide, dict):
            continue
        raw = [o for o in slide.get("findings") or [] if isinstance(o, dict)]
        if any(o.get("kind") == "overflow" for o in raw):
            # The frame clips at the canvas edge, so an element painted past
            # it is seen twice: as the frame's own cut and as the overflow.
            # One cut, one finding - the one that names the element.
            raw = [o for o in raw if not (o.get("kind") == "clipped" and o.get("block") == "slide")]
        index = int(slide.get("index") or 0)
        common = {
            "slide_index": index,
            "slide_id": slide.get("slide_id") or None,
            "layout": slide.get("layout") or None,
            "title": _slide_title(spec, index),
        }
        # An org chart is several findings at once (its fit, its lines, its counts...).
        org = [f for o in raw if o.get("kind") == "org_chart" for f in _org_chart_findings(o, common=common, unit=unit)]
        raw = [o for o in raw if o.get("kind") != "org_chart"]
        graded = [_finding_from_observation(o, slide=slide, spec=spec, unit=unit) for o in raw]
        balance = [
            f
            for o in raw
            if o.get("kind") == "sparse"
            for f in _balance_findings(o, spec=spec, common=common, unit=unit)
        ]
        panel_fill = [
            f
            for o in raw
            if o.get("kind") == "panel_fill"
            for f in [_panel_fill_finding(o, common=common, unit=unit)]
            if f is not None
        ]
        kept = [f for f in graded if f is not None] + balance + panel_fill + org
        kept += _org_hint_findings(spec, index, common)
        if any(f.kind == "unfilled" for f in kept):
            # The body asked for its room: the placement hints would name a
            # remedy it already chose. One finding, the renderer's.
            kept = [f for f in kept if f.kind not in {"sparse", "floating", "top_heavy"}]
        if any(f.kind == "floating" for f in kept):
            # A lone block floating under the title says more than "the
            # bottom is bare": one finding, the one with the placement remedy.
            kept = [f for f in kept if f.kind != "sparse"]
        if any(f.kind in _EXCESS_KINDS for f in kept):
            # Never both directions on one slide: a body that was
            # shrunk, cut or overflowing is not also told it left room - the
            # deficit remedies would contradict the cut it is asked for.
            # Graded findings, not raw observations: a fit ratio within a
            # reader's forgiveness is not a finding and does not silence one.
            kept = [f for f in kept if f.kind not in _DEFICIT_KINDS]
        if any(f.kind == "fit" for f in kept):
            # A shrunk body shrinks its logos with it: the cut is the
            # remedy, and "raise logo_height" would make the slide denser.
            kept = [f for f in kept if f.kind != "logo"]
        if any(f.kind == "chart" for f in kept):
            # A chart too short to read is told to take more height;
            # its values wanting that same height is the same remedy.
            short = {
                id(f)
                for o, f in zip(raw, graded, strict=True)
                if f is not None and f.kind == "values" and o.get("reason") == "short"
            }
            kept = [f for f in kept if id(f) not in short]
        # Two logo blocks drawn alike on one slide read the same sentence;
        # the agent is told once.
        told: set[str] = set()
        for finding in kept:
            if finding.kind == "logo":
                if finding.message in told:
                    continue
                told.add(finding.message)
            findings.append(finding)
    findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.slide_index, f.kind))
    return findings


# ── Running the check ────────────────────────────────────────────────────────


def _cache_key(presentation_id: uuid.UUID, user_id: uuid.UUID, revision: int) -> str:
    # Per user: the render runs with the caller's visibility (template assets,
    # theme), so two users may legitimately measure two different decks.
    return f"presentation:layout:{presentation_id}:{user_id}:{revision}"


async def _cached_report(key: str) -> LayoutReport | None:
    """A report kept from an earlier check of the same revision, or ``None``."""
    try:
        redis = await redis_client()
        raw = await redis.get(key)
    except Exception as exc:  # the cache is a courtesy - fail open to a render
        logger.debug("Presentation layout cache read skipped for %s: %s", key, format_exception_message(exc))
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        return LayoutReport.from_dict(payload) if isinstance(payload, dict) else None
    except (TypeError, ValueError):
        return None


async def _remember_report(key: str, report: LayoutReport) -> None:
    try:
        redis = await redis_client()
        await redis.set(key, json.dumps(report.as_dict()), ex=LAYOUT_CHECK_CACHE_TTL_SECS)
    except Exception as exc:
        logger.debug("Presentation layout cache write skipped for %s: %s", key, format_exception_message(exc))


async def check_presentation_layout(
    presentation_id: uuid.UUID,
    user_id: uuid.UUID,
    spec: dict,
    *,
    expected_revision: int | None = None,
    timeout: float = LAYOUT_CHECK_TIMEOUT_SECS,
) -> LayoutReport:
    """Render the saved deck as *user_id* sees it and measure every slide.

    Never raises for a render problem: the report says why it was skipped. The
    caller has already authorized *user_id* on the deck; the render token is
    minted for that user, so the page renders with THAT user's visibility.

    *expected_revision* is the revision *spec* belongs to. The render page
    loads the deck's CURRENT row, so when a save lands in between the page
    renders a deck the caller does not hold: the check is then skipped rather
    than reported against the wrong titles. It is also what makes a report
    reusable - the same revision is the same pixels, so a successful report is
    kept for ``LAYOUT_CHECK_CACHE_TTL_SECS`` and served to the next check of it
    (the read-back right after a save, the panel's re-check) without a browser.
    Without a revision every call renders.
    """
    unit = unit_noun(spec, 1)
    if not layout_check_enabled():
        return LayoutReport(slide_count=0, skipped="disabled", unit=unit)
    key = _cache_key(presentation_id, user_id, expected_revision) if expected_revision is not None else None
    if key is not None:
        cached = await _cached_report(key)
        if cached is not None:
            return cached
    from app.services.presentation_export import PresentationRenderError, render_deck

    aspect_ratio = str((spec or {}).get("aspect_ratio") or "16:9")
    try:
        rendered = await asyncio.wait_for(
            render_deck(presentation_id, user_id, aspect_ratio=aspect_ratio, want_layout=True),
            timeout=timeout,
        )
    except TimeoutError:
        logger.info("Presentation layout check: render of %s did not finish within %.0fs", presentation_id, timeout)
        return LayoutReport(
            slide_count=0, skipped=f"the render did not finish within {timeout:.0f} seconds", unit=unit
        )
    except PresentationRenderError as exc:
        logger.info(
            "Presentation layout check: render of %s failed: %s", presentation_id, format_exception_message(exc)
        )
        return LayoutReport(slide_count=0, skipped=str(exc), unit=unit)
    except Exception as exc:
        # No Chromium on this host, a browser crash: the check is a courtesy,
        # the save is not - log and move on.
        logger.warning(
            "Presentation layout check unavailable for %s: %s", presentation_id, format_exception_message(exc)
        )
        return LayoutReport(
            slide_count=0, skipped="the headless browser is not available on this deployment", unit=unit
        )
    revision = rendered.revision if rendered.revision is not None else expected_revision
    if expected_revision is not None and rendered.revision is not None and rendered.revision != expected_revision:
        logger.info(
            "Presentation layout check: %s changed while it was measured (revision %s -> %s)",
            presentation_id,
            expected_revision,
            rendered.revision,
        )
        return LayoutReport(
            slide_count=rendered.slide_count,
            skipped=(
                "the presentation changed while it was being measured "
                f"(revision {expected_revision} -> {rendered.revision}); check again"
            ),
            unit=unit,
            revision=rendered.revision,
        )
    report = LayoutReport(
        slide_count=rendered.slide_count,
        findings=tuple(findings_from_observations(rendered.layout, spec)),
        unit=unit,
        revision=revision,
    )
    if key is not None:
        await _remember_report(key, report)
    return report


# ── The words the agent reads ────────────────────────────────────────────────


def narrow_layout_report(
    report: LayoutReport, *, slide_ids: set[str], slide_indexes: set[int]
) -> tuple[LayoutReport, int]:
    """*report* restricted to the slides an edit wrote, plus how many findings it left out.

    A surgical edit must not turn into a deck-wide clean-up: the agent is told
    what its OWN change did to the slides it touched, and only counted the
    pre-existing findings on the slides it did not — those belong to a
    separate request, and acting on them unasked would break the promise that
    "change one word" leaves the other slides alone. A finding is matched by
    the slide's stable id when the render published one and by its position in
    the measured revision otherwise; a skipped report is returned as is (its
    findings are meaningless). ``slide_count`` becomes the number of written
    slides so a clean line speaks about them, not the deck.
    """
    if report.skipped:
        return report, 0
    kept = tuple(
        finding
        for finding in report.findings
        if (finding.slide_id and finding.slide_id in slide_ids) or finding.slide_index in slide_indexes
    )
    narrowed = LayoutReport(
        slide_count=len(slide_indexes),
        findings=kept,
        skipped=None,
        unit=report.unit,
        revision=report.revision,
    )
    return narrowed, len(report.findings) - len(kept)


def format_layout_report(report: LayoutReport) -> str:
    """The ``Layout check`` section of a tool result (empty string when clean).

    One line per finding, slide number first (the agent's own numbering is
    1-based, as in every validation error), the layout in brackets and the
    title quoted so the slide is unambiguous even after a reorder. The list
    ends with the instruction, because the finding is only useful if it is
    acted on in the same turn. A page brief reads "page", never "slide".
    """
    unit = report.unit or "slide"
    if report.skipped == "disabled":
        return ""
    if report.skipped:
        return f"Layout check skipped: {report.skipped}. Keep each {unit} within the density budget."
    if not report.findings:
        units = unit if report.slide_count == 1 else f"{unit}s"
        return f"Layout check: every one of the {report.slide_count} {units} fits its canvas."
    lines = [
        f"Layout check: {len(report.findings)} {unit} issue(s) the audience would see. Fix them and save again:"
    ]
    for finding in report.findings[:MAX_REPORTED_FINDINGS]:
        where = f"{unit} {finding.slide_index + 1}"
        if finding.layout:
            where += f" [{finding.layout}]"
        if finding.title:
            where += f' "{finding.title}"'
        lines.append(f"- {where} ({finding.severity}): {finding.message}")
    hidden = len(report.findings) - MAX_REPORTED_FINDINGS
    if hidden > 0:
        lines.append(f"- ...and {hidden} more; fix the above, save, and the next check lists the rest.")
    return "\n".join(lines)


__all__ = [
    "CHART_MIN_HEIGHT_PX",
    "CUT_OVERLOADED_PX",
    "FIT_DENSE_BELOW",
    "FIT_FLOOR",
    "FIT_OVERLOADED_BELOW",
    "LAYOUT_CHECK_CACHE_TTL_SECS",
    "LAYOUT_CHECK_TIMEOUT_SECS",
    "LOGO_MIN_COVERAGE",
    "LOGO_MIN_HEIGHT_PX",
    "MAX_REPORTED_FINDINGS",
    "SPARSE_ABOVE",
    "LayoutFinding",
    "LayoutReport",
    "check_presentation_layout",
    "findings_from_observations",
    "format_layout_report",
    "layout_check_enabled",
    "narrow_layout_report",
    "sparse_remedy",
]
