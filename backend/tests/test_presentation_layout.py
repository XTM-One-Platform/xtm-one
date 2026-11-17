"""The deck layout check: rendered, measured, reported to the agent.

``validate_spec`` guarantees a deck is well-formed; whether six cards and a
callout FIT on a 1920x1080 slide is a property of pixels, which only a render
knows. These tests lock in the two deterministic answers:

1. On screen, ``fit.ts`` shrinks a body that does not fit (PowerPoint's autofit)
   and publishes the ratio on the DOM as ``data-fit``.
2. After every save, the deck is rendered headless, ``LAYOUT_AUDIT_JS`` measures
   each slide, and the observations become findings in the tool result the agent
   reads in the same turn - which slide, which block, what to do.

The check is advisory: it never fails a save, never raises into the tool, and
says why when it could not run. No browser here: the render is faked, the
classification and the words are what is tested.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest

from app.services import presentation_layout as layout
from app.services.presentation_layout import (
    CUT_OVERLOADED_PX,
    FIT_DENSE_BELOW,
    FIT_FLOOR,
    FIT_OVERLOADED_BELOW,
    LAYOUT_CHECK_CACHE_TTL_SECS,
    MAX_REPORTED_FINDINGS,
    SPARSE_ABOVE,
    LayoutFinding,
    LayoutReport,
    check_presentation_layout,
    findings_from_observations,
    format_layout_report,
)
from app.services.presentation_spec import Size

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend"
PRESENTATION_DIR = FRONTEND_ROOT / "src" / "components" / "presentation"


def _spec(titles: list[str]) -> dict:
    return {"title": "Deck", "slides": [{"layout": "content", "title": t} for t in titles]}


def _slide(index: int, findings: list[dict], *, layout_name: str = "content") -> dict:
    return {"index": index, "slide_id": f"s{index}", "layout": layout_name, "findings": findings}


@pytest.fixture
def layout_enabled(monkeypatch):
    """The suite disables the check (conftest); the tests OF it turn it on."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "presentation_layout_check_enabled", True)


# ── Observations -> findings ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("scale", "expected"),
    [
        (1.0, None),
        (0.95, None),  # within a reader's forgiveness
        (FIT_DENSE_BELOW, None),  # the threshold itself is not a finding
        (0.85, "dense"),
        (FIT_OVERLOADED_BELOW, "dense"),
        (0.7, "overloaded"),
        (0.35, "overloaded"),
    ],
)
def test_fit_ratio_is_graded_against_the_two_thresholds(scale, expected):
    findings = findings_from_observations([_slide(0, [{"kind": "fit", "scale": scale}])], _spec(["A"]))
    if expected is None:
        assert findings == []
        return
    (finding,) = findings
    assert finding.kind == "fit"
    assert finding.severity == expected
    assert finding.scale == round(scale, 3)
    # The excess is stated as height the slide does not have: 0.8 -> 25%.
    assert finding.amount == round((1 / scale - 1) * 100)
    assert f"{finding.amount}% more height" in finding.message
    assert f"shrunk to {round(scale * 100)}%" in finding.message
    # The remedy is content, never styling.
    assert "Cut content" in finding.message


@pytest.mark.parametrize(
    ("amount", "expected"),
    [(7, "dense"), (CUT_OVERLOADED_PX - 1, "dense"), (CUT_OVERLOADED_PX, "overloaded"), (120, "overloaded")],
)
def test_a_cut_is_overloaded_from_a_row_of_text_up(amount, expected):
    observation = {"kind": "clipped", "side": "bottom", "amount": amount, "block": "table-wrap", "text": "Q4 revenue"}
    (finding,) = findings_from_observations([_slide(2, [observation])], _spec(["A", "B", "Third"]))
    assert finding.kind == "clipped"
    assert finding.severity == expected
    assert finding.amount == amount
    assert finding.slide_index == 2
    assert finding.title == "Third"
    assert finding.slide_id == "s2"
    assert finding.layout == "content"
    # The block is named as the agent knows it, the text quoted so it is found.
    assert 'the table wrap "Q4 revenue" is cut off at its bottom edge by' in finding.message
    assert f"by {amount}px" in finding.message


def test_each_kind_has_its_own_remedy():
    observations = [
        {"kind": "nowrap", "side": "right", "amount": 30, "block": "badge", "text": "Enterprise plan"},
        {"kind": "overflow", "side": "bottom", "amount": 55, "block": "card", "text": "A long card"},
        {"kind": "clipped", "side": "right", "amount": 12, "block": "chrome-footer", "text": "Confidential"},
    ]
    findings = findings_from_observations([_slide(0, observations)], _spec(["A"]))
    by_kind = {f.kind: f for f in findings}
    assert set(by_kind) == {"nowrap", "overflow", "clipped"}
    assert 'the badge "Enterprise plan" does not wrap and runs 30px past its box' in by_kind["nowrap"].message
    assert "Shorten the label or use fewer of them" in by_kind["nowrap"].message
    assert 'the card "A long card" is painted 55px past the bottom edge of the slide' in by_kind["overflow"].message
    # The chrome footer is the agent's ``footer_text``; it is named as such.
    assert by_kind["clipped"].message.startswith("the footer text is cut off at its right edge by 12px")


def test_quoted_text_is_collapsed_and_bounded():
    text = "word " * 40
    observation = {"kind": "nowrap", "amount": 20, "block": "badge", "text": text}
    (finding,) = findings_from_observations([_slide(0, [observation])], _spec(["A"]))
    quoted = re.search(r'"([^"]*)"', finding.message)
    assert quoted
    assert len(quoted.group(1)) <= 60
    assert quoted.group(1).endswith("...")
    assert "  " not in quoted.group(1)


def test_garbage_observations_are_ignored_not_raised():
    observations = [
        {"kind": "fit", "scale": "not-a-number"},
        {"kind": "fit"},
        {"kind": "clipped", "amount": "??", "block": None, "text": None},
        {"kind": "unknown"},
        "not a dict",
        None,
    ]
    findings = findings_from_observations(
        [_slide(0, observations), "not a slide", {"index": 1, "findings": None}],
        _spec(["A", "B"]),
    )
    # The malformed clip still yields a finding (amount 0, block "element").
    assert [f.kind for f in findings] == ["clipped"]
    assert findings[0].amount == 0
    assert "the element is cut off" in findings[0].message


def test_at_the_floor_the_finding_says_what_is_cut_off():
    """``fit.ts`` never scales below ``FIT_MIN``; the host clips the rest. The
    ratio alone would then understate the problem (the body needed MORE than
    the ratio says), so the finding names the floor and the cut the audit
    measured - what the audience actually loses."""
    (finding,) = findings_from_observations([_slide(0, [{"kind": "fit", "scale": FIT_FLOOR, "cut": 120}])], _spec(["A"]))
    assert finding.severity == "overloaded"
    assert finding.scale == FIT_FLOOR
    assert "needed at least 186% more height" in finding.message
    assert "shrunk to 35%, the floor" in finding.message
    assert "what still does not fit (120px) is cut off at the bottom" in finding.message
    assert "Cut content" in finding.message

    # An older audit script (no ``cut``) or a cut within tolerance: the floor
    # is still named, the amount is not invented.
    (finding,) = findings_from_observations([_slide(0, [{"kind": "fit", "scale": FIT_FLOOR}])], _spec(["A"]))
    assert "the floor - what still does not fit is cut off at the bottom" in finding.message
    assert "px" not in finding.message

    # Above the floor nothing is clipped: the ordinary wording, no cut.
    (finding,) = findings_from_observations([_slide(0, [{"kind": "fit", "scale": 0.6, "cut": 0}])], _spec(["A"]))
    assert "the floor" not in finding.message
    assert "so nothing overlaps" in finding.message


def test_a_page_brief_is_told_about_its_pages():
    """A portrait brief has pages; ``unit_noun`` already says so in every other
    tool result, and a finding that calls them slides would contradict it."""
    spec = {**_spec(["Cover", "Body"]), "aspect_ratio": "A4"}
    findings = findings_from_observations(
        [
            _slide(1, [{"kind": "fit", "scale": 0.7}]),
            _slide(0, [{"kind": "overflow", "side": "bottom", "amount": 30, "block": "card", "text": "x"}]),
        ],
        spec,
    )
    by_kind = {f.kind: f for f in findings}
    assert "more height than the page has" in by_kind["fit"].message
    assert "move part of it to a new page." in by_kind["fit"].message
    assert "past the bottom edge of the page" in by_kind["overflow"].message
    assert "slide" not in by_kind["fit"].message and "slide" not in by_kind["overflow"].message

    report = LayoutReport(slide_count=2, findings=tuple(findings), unit="page")
    lines = format_layout_report(report).splitlines()
    assert lines[0] == "Layout check: 2 page issue(s) the audience would see. Fix them and save again:"
    assert lines[1].startswith('- page 2 [content] "Body" (overloaded): the body needed 43% more height')
    assert format_layout_report(LayoutReport(slide_count=2, unit="page")) == (
        "Layout check: every one of the 2 pages fits its canvas."
    )
    assert format_layout_report(LayoutReport(slide_count=1, unit="page")) == (
        "Layout check: every one of the 1 page fits its canvas."
    )
    assert format_layout_report(LayoutReport(slide_count=0, skipped="x", unit="page")).endswith(
        "Keep each page within the density budget."
    )


def test_an_overflow_is_not_also_reported_as_the_frames_own_cut():
    """The slide frame clips at the canvas edge, so a card painted past the
    bottom is seen twice by the script: as the frame's ``clipped`` and as the
    card's ``overflow``. One cut, one finding - the one naming the card. A
    clip by any OTHER box on the same slide is a different cut and stays."""
    observations = [
        {"kind": "clipped", "side": "bottom", "amount": 120, "block": "slide", "text": "A long card"},
        {"kind": "overflow", "side": "bottom", "amount": 120, "block": "card", "text": "A long card"},
        {"kind": "clipped", "side": "bottom", "amount": 30, "block": "table-wrap", "text": "row 9"},
    ]
    findings = findings_from_observations([_slide(0, observations)], _spec(["A"]))
    assert sorted((f.kind, f.amount) for f in findings) == [("clipped", 30), ("overflow", 120)]
    # Without the overflow the frame's cut is the only trace and is kept.
    findings = findings_from_observations([_slide(0, observations[:1])], _spec(["A"]))
    assert [f.kind for f in findings] == ["clipped"]


# A content slide holding one six-item feature_list, packed under the title,
# more than half of its body bare (above ``SPARSE_ABOVE``). The
# script reports the share of the BODY left empty (0.62 of a 750px body is
# 465px) and the slide's height (1080), so the sentence states the share of
# the slide the room sees (43%).
_SPARSE_BODY = {"kind": "sparse", "unused": 0.62, "empty": 465, "slide": 1080}
_SIX_FEATURES = {"type": "feature_list", "items": [{"icon": "shield", "title": f"Module {n}"} for n in range(1, 7)]}


def _content(title: str, *blocks: dict, **fields) -> dict:
    return {"layout": "content", "title": title, "blocks": list(blocks), **fields}


def _deck(*slides: dict) -> dict:
    return {"title": "Deck", "slides": list(slides)}


def test_a_content_slide_that_leaves_the_bottom_bare_is_reported_sparse():
    spec = _deck(_content("A"), _content("B"), _content("Features", _SIX_FEATURES), _content("D"))
    (finding,) = findings_from_observations([_slide(2, [_SPARSE_BODY])], spec)
    assert finding.kind == "sparse"
    assert finding.severity == "advisory"
    assert finding.scale is None
    assert finding.amount == 43
    assert finding.title == "Features"
    # Blocks are sized to their content: the remedies never stretch
    # the grid; content, a footer_block takeaway, a merge with the next slide.
    assert finding.message == (
        "the body leaves 43% of the slide empty below the last block. Add content to the body, "
        "add a footer_block takeaway, or merge with the next slide."
    )
    # The line the agent reads names the slide, the layout and the remedies.
    report = LayoutReport(slide_count=4, findings=(finding,))
    lines = format_layout_report(report).splitlines()
    assert lines[1].startswith('- slide 3 [content] "Features" (advisory): the body leaves 43% of the slide empty')
    assert "Add content to the body, add a footer_block takeaway, or merge with the next slide." in lines[1]


def test_the_sparse_remedies_never_stretch_a_block():
    """Blocks are sized to their content: the remedies are content, a
    `footer_block` takeaway where there is none and the merge with the
    neighbour that exists - never `fill` or `distribute`, whatever the slide
    holds."""
    from app.services.presentation_layout import sparse_remedy

    text = {"type": "text", "content": "One paragraph."}
    callout = {"type": "callout", "content": "Note"}
    grid = _content("Grid", _SIX_FEATURES, {"type": "list", "items": ["a", "b"]})
    columns = {"layout": "content", "title": "Cols", "columns": [[text, callout], [_SIX_FEATURES]]}
    for slide in (grid, columns, _content("Two", text, callout), _content("Lone", _SIX_FEATURES, distribute=True)):
        remedy = sparse_remedy(_deck(slide, _content("B")), 0, "slide")
        assert remedy == "Add content to the body, add a footer_block takeaway, or merge with the next slide."
        assert "fill" not in remedy.lower().replace("footer_block", "")
        assert "distribute" not in remedy
    # The last slide merges with the previous one; the noun follows the deck.
    spec = _deck(_content("A"), _content("Last", text))
    assert sparse_remedy(spec, 1, "page") == "Add content to the body, add a footer_block takeaway, or merge with the previous page."
    # A footer block already there is not offered again; a lone slide has no neighbour.
    spec = _deck(_content("Only", text, footer_block={"type": "text", "content": "Source"}))
    assert sparse_remedy(spec, 0, "slide") == "Add content to the body."


def test_a_slide_closed_by_a_footer_block_is_never_sparse():
    """A `footer_block` closes a slide that ends early on purpose."""
    footer = {"type": "callout", "content": "The takeaway"}
    spec = _deck(_content("Closed", _SIX_FEATURES, footer_block=footer), _content("B"))
    assert findings_from_observations([_slide(0, [_SPARSE_BODY])], spec) == []
    # The same slide without it is reported.
    spec = _deck(_content("Open", _SIX_FEATURES), _content("B"))
    assert [f.kind for f in findings_from_observations([_slide(0, [_SPARSE_BODY])], spec)] == ["sparse"]


@pytest.mark.parametrize(
    "unused",
    # A body left up to half empty is a slide that ends early: 0.33,
    # 0.45 and the threshold itself are not findings; garbage is not either.
    [0.0, 0.1, 0.33, 0.45, 0.50, SPARSE_ABOVE, 1.01, -0.2],
)
def test_a_body_filled_past_the_threshold_is_not_sparse(unused):
    observation = {"kind": "sparse", "unused": unused, "empty": 200, "slide": 1080}
    assert findings_from_observations([_slide(0, [observation])], _spec(["A"])) == []


def test_the_sparse_measure_reads_the_room_and_only_a_deliberate_placement_silences_it():
    """A ``vertical_align`` of center / end places the run on purpose and the
    script emits no ``sparse`` observation for that slide (``data-valign`` is
    what it reads), so a slide with no observation has no finding - unless the
    slide also sets ``distribute``, which wins over the placement in the
    renderer (the run is spread from the top, as the schema says) and so wins
    here: a centred slide that distributes is measured like any other. ``fill``
    and ``distribute`` silence NOTHING: their spread is capped at
    a readable gap (``spread.ts``), so the run's box is no longer the room and
    the floor it used to reach can be bare. The room is therefore the
    ``.deck-fit`` host - the space between the header and the footer block -
    and the content ends at the blocks' bottom edge: the run's children on the
    single-flow form, each column's blocks on the columns form (the columns
    themselves stretch). The shapes are pinned on the script itself."""
    from app.services.presentation_export import LAYOUT_AUDIT_JS

    assert findings_from_observations([_slide(0, [])], _spec(["A"])) == []
    # Content slides only: the other layouts compose their own body.
    assert 'frame.dataset.layout === "content"' in LAYOUT_AUDIT_JS
    # The body host (the blocks run or the columns grid) locates the slide's
    # body; the ROOM is the fit host around it.
    assert '".deck-fit-content > .deck-blocks-slide, .deck-fit-content > .deck-columns-slide"' in LAYOUT_AUDIT_JS
    assert 'const room = host && host.closest(".deck-fit");' in LAYOUT_AUDIT_JS
    sparse = LAYOUT_AUDIT_JS[LAYOUT_AUDIT_JS.index("The other direction: a content body") : LAYOUT_AUDIT_JS.index('kind: "sparse"')]
    code = "\n".join(line for line in sparse.splitlines() if not line.strip().startswith("//"))
    # The one silence: a deliberate placement the spread switch has not
    # overridden. `distribute` appears in the guard ONLY as that override -
    # never as an exemption of its own - and `fill` not at all.
    placed = (
        'const placed = host.dataset.distribute !== "true"\n'
        '          && (host.dataset.valign === "center" || host.dataset.valign === "end");'
    )
    assert placed in code
    assert code.count("dataset.distribute") == 1
    assert "dataset.fill" not in code
    # The blocks' bottom edge - each column's blocks on the columns form -
    # against the room's, never a scroll height.
    assert '":scope > .deck-column > .deck-blocks > *"' in code
    assert "last = Math.max(last, cr.bottom)" in code
    assert "scrollHeight" not in code
    assert 'findings.push({ kind: "sparse", unused: empty / rr.height, empty:' in LAYOUT_AUDIT_JS


def test_the_spread_is_bounded_by_the_renderer_not_by_the_agent():
    """The fix for a thin slide stretched to the floor is deterministic
    code, not prompt text: every row-spreading grid root (``list``,
    ``metrics_grid``, ``key_value``, ``feature_list``, ``personas``, and the
    ``table``, whose rows themselves grow and are read by a
    measurer of its own) and the
    ``distribute`` run (``BlockList``, on the slide and in every column) run
    ``useSpreadCap``, which writes an inline ``max-height`` of natural height
    plus one readable extra per gap and publishes it as ``data-spread-cap``.
    The extras are canvas pixels, pinned here so a change to them is a
    deliberate one made where the agent-facing words can follow. The takeaways
    ``numbered_cards`` keep sharing the height - their cards, not their gaps,
    take the room - but each row of cards is capped too, one readable
    extra per ROW read off the cards at their natural height; the figures
    keep filling the room."""
    spread = (PRESENTATION_DIR / "spread.ts").read_text(encoding="utf-8")
    maths = (PRESENTATION_DIR / "spread-math.ts").read_text(encoding="utf-8")
    assert "export const SPREAD_ROW_MAX_EXTRA_PX = 48;" in maths
    assert "export const SPREAD_BLOCK_MAX_EXTRA_PX = 64;" in maths
    assert "export const SPREAD_CARD_MAX_EXTRA_PX = 96;" in maths
    assert 'const growing = grows === "rows" ? rows.length : gaps;' in maths
    assert "export function spreadCap(" in maths
    assert "export function useSpreadCap(" in spread
    assert "el.style.maxHeight = `${px}px`" in spread
    assert "el.dataset.spreadCap = px" in spread
    # A filler child (a fill figure, a grow spacer) opts the run out of the cap.
    assert "getComputedStyle(child).flexGrow) > 0) filler = true" in spread
    blocks_dir = PRESENTATION_DIR / "blocks"
    wired = {
        "list.tsx": "useSpreadCap(rootRef, block.fill, SPREAD_ROW_MAX_EXTRA_PX, block.items)",
        "metrics.tsx": "useSpreadCap(rootRef, block.fill, SPREAD_ROW_MAX_EXTRA_PX, block.items)",
        "cards.tsx": "useSpreadCap(rootRef, block.fill, SPREAD_ROW_MAX_EXTRA_PX, block.items)",
        "table.tsx": "useSpreadCap(rootRef, block.fill, SPREAD_ROW_MAX_EXTRA_PX, block.rows, measureTableRows)",
        # A distribute run of cards, timelines or grids spreads free (#10228):
        # only a run that holds text, a list or a quote is capped.
        "index.tsx": "useSpreadCap(runRef, distribute && !free, SPREAD_BLOCK_MAX_EXTRA_PX, blocks)",
    }
    for name, call in wired.items():
        assert call in (blocks_dir / name).read_text(encoding="utf-8"), name
    run_source = (blocks_dir / "index.tsx").read_text(encoding="utf-8")
    assert "const free = !!distribute && spreadsFree(blocks);" in run_source
    brief = (blocks_dir / "brief.tsx").read_text(encoding="utf-8")
    assert brief.count("useSpreadCap(rootRef, block.fill, SPREAD_ROW_MAX_EXTRA_PX, block.items)") == 1  # personas
    # key_value grows its ROWS (each pair centred between its rules), read at
    # their natural height like the cards.
    assert "useSpreadCap(rootRef, block.fill, SPREAD_ROW_MAX_EXTRA_PX, block.items, measureCardRows)" in brief
    # numbered_cards grows its cards, not its gaps - bounded per row of cards,
    # measured with the grid at its content height (the takeaways body and a
    # `fill` block alike).
    cards = (blocks_dir / "cards.tsx").read_text(encoding="utf-8")
    numbered = cards.split("export function NumberedCards(", 1)[1]
    assert "useSpreadCap(rootRef, fill, SPREAD_CARD_MAX_EXTRA_PX, items, measureCardRows)" in numbered
    assert "export function measureCardRows(" in spread
    assert 'grows: "rows"' in spread.split("export function measureCardRows(", 1)[1]
    # On the takeaways slide the cards are the body: what their cap leaves is
    # shared above and below them.
    css = (PRESENTATION_DIR / "presentation.css").read_text(encoding="utf-8")
    assert '.deck-root .deck-fit-content > .deck-cards[data-fill="true"] { margin-block: auto; }' in css
    # Each column's run is a distribute run too - unless the columns line
    # their blocks up as rows, which are not spread.
    body = (PRESENTATION_DIR / "layouts" / "body.tsx").read_text(encoding="utf-8")
    assert "const spread = slide.distribute && !slide.equal_rows;" in body
    assert "<BlockList blocks={column} distribute={spread} path={`columns.${index}`} />" in body


def test_a_dense_slide_is_never_also_sparse():
    """Never both directions on one slide: a body that was shrunk, cut or
    overflowing is not also told it left room. Decided on GRADED findings - a
    fit ratio within a reader's forgiveness is not a finding and does not
    silence the sparse one."""
    for excess in (
        {"kind": "fit", "scale": 0.8},
        {"kind": "clipped", "side": "bottom", "amount": 20, "block": "card", "text": "x"},
        {"kind": "nowrap", "side": "right", "amount": 12, "block": "badge", "text": "y"},
        {"kind": "overflow", "side": "bottom", "amount": 30, "block": "card", "text": "z"},
    ):
        findings = findings_from_observations([_slide(0, [_SPARSE_BODY, excess])], _spec(["A"]))
        assert [f.kind for f in findings] == [excess["kind"]], excess
    findings = findings_from_observations([_slide(0, [_SPARSE_BODY, {"kind": "fit", "scale": 0.95}])], _spec(["A"]))
    assert [f.kind for f in findings] == ["sparse"]
    # The exclusion is per slide: a dense slide elsewhere does not hide it.
    observations = [_slide(0, [{"kind": "fit", "scale": 0.8}]), _slide(1, [_SPARSE_BODY])]
    findings = findings_from_observations(observations, _spec(["A", "B"]))
    assert [(f.kind, f.slide_index) for f in findings] == [("fit", 0), ("sparse", 1)]


def test_the_sparse_share_falls_back_to_the_body_share_without_a_slide_height():
    observation = {"kind": "sparse", "unused": 0.6}
    (finding,) = findings_from_observations([_slide(0, [observation])], _spec(["A"]))
    assert finding.amount == 60


# Balance: the ``sparse`` observation also carries the room's height
# (``room``), the count of blocks drawn (``blocks``) and, on the columns form,
# where each column's blocks end (``ends``, px from the top of the room).
_KPI = {"type": "metrics_grid", "items": [{"value": "42%", "label": "fewer alerts"}], "columns": 1}
_CARD = {"type": "callout", "content": "One short card"}


def _columns(title: str, *columns: list, **fields) -> dict:
    return {"layout": "content", "title": title, "columns": list(columns), **fields}


def _balance(unused: float, *, blocks: int, ends: list | None = None, room: int = 750) -> dict:
    empty = round(unused * room)
    return {
        "kind": "sparse",
        "unused": unused,
        "empty": empty,
        "slide": 1080,
        "room": room,
        "blocks": blocks,
        "ends": ends or [],
    }


def test_columns_that_end_far_apart_are_reported_uneven_with_remedies_read_off_the_slide():
    """Four KPI tiles beside one short card: the right column stops far above
    the left one. Advisory, the shorter column named, `equal_height` offered
    because each column is one card - never a stretch to the floor."""
    spec = _deck(_columns("KPIs", [_KPI], [_CARD]), _content("B"))
    observation = _balance(0.2, blocks=2, ends=[600, 150])
    findings = findings_from_observations([_slide(0, [observation])], spec)
    assert [f.kind for f in findings] == ["uneven"]
    (finding,) = findings
    assert finding.severity == "advisory"
    assert finding.amount == 60
    assert finding.message == (
        "the columns end 60% of the body apart - column 2 stops 450px above the longest one. "
        "Add a block to column 2 (a metric, a list or a takeaway card), set equal_height: true so the cards "
        "share the tallest one's height, or move a block from the longer column to it."
    )
    assert "fill" not in finding.message
    assert "distribute" not in finding.message
    # Shared heights are already asked for: not offered again.
    spec = _deck(_columns("KPIs", [_KPI], [_CARD], equal_height=True), _content("B"))
    (finding,) = findings_from_observations([_slide(0, [observation])], spec)
    assert "equal_height" not in finding.message
    # Several blocks per column: shared card heights are no remedy.
    spec = _deck(_columns("KPIs", [_KPI, _CARD], [_CARD]), _content("B"))
    (finding,) = findings_from_observations([_slide(0, [observation])], spec)
    assert "equal_height" not in finding.message


@pytest.mark.parametrize("ends", [[600, 460], [600, 600], [600], [600, None], []])
def test_columns_that_end_close_together_are_balanced(ends):
    """Within a fifth of the body (150px of 750) the bottoms read as one
    line; a single drawn column has nothing to be uneven against."""
    spec = _deck(_columns("KPIs", [_KPI], [_CARD]), _content("B"))
    observation = _balance(0.2, blocks=2, ends=ends)
    assert findings_from_observations([_slide(0, [observation])], spec) == []


def test_a_lone_block_packed_under_the_title_is_reported_floating_once():
    """One card under the title over a bare body: `floating` (a placement or
    the content that carries it), never also `sparse` on the same slide."""
    spec = _deck(_content("Poll", _CARD), _content("B"))
    observation = _balance(0.62, blocks=1)
    findings = findings_from_observations([_slide(0, [observation])], spec)
    assert [f.kind for f in findings] == ["floating"]
    (finding,) = findings
    assert finding.severity == "advisory"
    assert finding.amount == 43
    assert finding.message == (
        "the body is one block packed under the title, 43% of the slide bare below it. "
        "Set vertical_align: center, or add the metric, list or visual that carries it."
    )
    # Under the sparse threshold it is still a floating block.
    (finding,) = findings_from_observations([_slide(0, [_balance(0.4, blocks=1)])], spec)
    assert finding.kind == "floating"


@pytest.mark.parametrize("unused, blocks", [(0.25, 1), (0.30, 1), (0.4, 2), (0.4, 0)])
def test_a_full_or_composed_body_is_not_floating(unused, blocks):
    spec = _deck(_content("Poll", _CARD), _content("B"))
    findings = findings_from_observations([_slide(0, [_balance(unused, blocks=blocks)])], spec)
    assert "floating" not in [f.kind for f in findings]


def test_a_fill_lone_block_short_of_the_floor_is_unfilled_not_floating():
    """A `fill` grid that asked for the body and stops far above the floor is
    `unfilled`, dense, one finding - never also the
    `floating` placement hint, whose remedy the slide did not choose."""
    grid = {**_SIX_FEATURES, "fill": True}
    spec = _deck(_content("Features", grid), _content("B"))
    (finding,) = findings_from_observations([_slide(0, [_balance(0.42, blocks=1)])], spec)
    assert finding.kind == "unfilled"
    assert finding.severity == "dense"
    assert "(fill: true)" in finding.message


@pytest.mark.parametrize(
    "slide",
    [
        _content("Stretch", _CARD, _CARD, vertical_align="stretch"),
        _content("Spread", _CARD, _CARD, distribute=True),
        _columns("Cols", [{**_CARD, "fill": True}], [_CARD], equal_height=True),
    ],
)
def test_a_body_that_asks_to_fill_and_leaves_a_bottom_band_is_unfilled(slide):
    """stretch, distribute or a `fill` block with more than a tenth of the
    body bare under it: an error grade (`dense`), whatever the band's size,
    and the placement hints are silenced."""
    spec = _deck(slide, _content("B"))
    for unused in (0.15, 0.4, 0.62):
        findings = findings_from_observations([_slide(0, [_balance(unused, blocks=2)])], spec)
        assert [f.kind for f in findings] == ["unfilled"], unused
        assert findings[0].severity == "dense"
        # Short content under a bounded spread is the content's limit, not a
        # renderer defect: the remedy is a placement or more content.
        assert "renderer defect" not in findings[0].message
        assert "Set vertical_align: center instead" in findings[0].message
    # Within a tenth of the body, the request is honoured.
    assert findings_from_observations([_slide(0, [_balance(0.08, blocks=2)])], spec) == []


def test_a_body_that_asks_nothing_keeps_the_placement_hints():
    spec = _deck(_content("Banners", _CARD, _CARD), _content("B"))
    findings = findings_from_observations([_slide(0, [_balance(0.15, blocks=2)])], spec)
    assert findings == []


@pytest.mark.parametrize("block, label", [("table", "fill table"), ("map", "map")])
def test_a_fill_table_or_map_short_of_its_panel_is_unfilled(block, label):
    spec = _deck({"layout": "dashboard", "title": "Board"}, _content("B"))
    observation = {"kind": "panel_fill", "block": block, "panel": 2, "unused": 0.45}
    (finding,) = findings_from_observations([_slide(0, [observation], layout_name="dashboard")], spec)
    assert finding.kind == "unfilled"
    assert finding.severity == "dense"
    assert finding.amount == 45
    assert finding.message.startswith(f"the {label} in panel 2 leaves 45% of the panel empty")
    # Within a tenth of the panel: filled.
    observation = {**observation, "unused": 0.05}
    assert findings_from_observations([_slide(0, [observation], layout_name="dashboard")], spec) == []


def test_the_audit_script_measures_a_fill_table_and_a_map_against_their_panel():
    from app.services.presentation_export import LAYOUT_AUDIT_JS

    assert "panel.querySelector('.deck-table-block[data-fill=\"true\"] .deck-table')" in LAYOUT_AUDIT_JS
    assert "panel.querySelector('.deck-chart[data-kind=\"map\"] .deck-chart-canvas')" in LAYOUT_AUDIT_JS
    assert 'findings.push({ kind: "panel_fill", block, panel: index + 1,' in LAYOUT_AUDIT_JS
    panel = (PRESENTATION_DIR / "PresentationLayoutPanel.tsx").read_text(encoding="utf-8")
    assert 'unfilled: "Does not fill the room it asked for",' in panel


def test_a_body_of_several_blocks_in_the_top_two_thirds_is_top_heavy():
    """Over a third of the body bare, under the sparse threshold: advisory
    `top_heavy` with the placement remedy; over the threshold it stays `sparse`."""
    spec = _deck(_content("Banners", _CARD, _CARD), _content("B"))
    findings = findings_from_observations([_slide(0, [_balance(0.4, blocks=2)])], spec)
    assert [f.kind for f in findings] == ["top_heavy"]
    (finding,) = findings
    assert finding.severity == "advisory"
    assert finding.message == (
        "the body sits in the top of the slide, 28% of it bare below the last block. "
        "Set vertical_align: center, or add the block that carries the claim."
    )
    assert [f.kind for f in findings_from_observations([_slide(0, [_balance(0.62, blocks=2)])], spec)] == ["sparse"]


@pytest.mark.parametrize("unused, blocks, ends", [(0.30, 2, None), (0.4, 1, None), (0.4, 2, [300, 290])])
def test_a_balanced_lone_or_columns_body_is_not_top_heavy(unused, blocks, ends):
    spec = _deck(_content("Banners", _CARD, _CARD), _content("B"))
    findings = findings_from_observations([_slide(0, [_balance(unused, blocks=blocks, ends=ends)])], spec)
    assert "top_heavy" not in [f.kind for f in findings]


def test_a_dense_slide_is_never_also_top_heavy():
    spec = _deck(_content("Banners", _CARD, _CARD), _content("B"))
    observations = [_slide(0, [_balance(0.4, blocks=2), {"kind": "fit", "scale": 0.8}])]
    assert [f.kind for f in findings_from_observations(observations, spec)] == ["fit"]


def test_a_card_title_on_two_lines_is_reported_wrapped():
    spec = _deck(_content("Team", _CARD), _content("B"))
    observation = {"kind": "wrapped", "lines": 2, "block": "profile-name", "text": "A long name"}
    (finding,) = findings_from_observations([_slide(0, [observation])], spec)
    assert finding.kind == "wrapped"
    assert finding.severity == "advisory"
    assert finding.amount == 2
    assert finding.message == (
        'the profile name "A long name" wraps to 2 lines - a title reads on one. '
        "Shorten it to its key words, or use fewer columns so each card is wider."
    )
    # One line is the design.
    one = {**observation, "lines": 1}
    assert findings_from_observations([_slide(0, [one])], spec) == []


def test_wrapped_titles_are_measured_by_the_audit_script():
    from app.api.v1.schemas.presentation import PresentationLayoutFinding
    from app.services.presentation_export import LAYOUT_AUDIT_JS

    assert ".deck-card-title, .deck-feature-title, .deck-persona-name, .deck-tier-name, .deck-profile-name" in LAYOUT_AUDIT_JS
    assert 'findings.push({ kind: "wrapped", lines, block, text: excerpt(title.textContent) });' in LAYOUT_AUDIT_JS
    assert {"top_heavy", "wrapped"} <= set(get_args(layout.FindingKind))
    description = PresentationLayoutFinding.model_fields["kind"].description or ""
    assert "top_heavy" in description and "wrapped" in description
    panel = (PRESENTATION_DIR / "PresentationLayoutPanel.tsx").read_text(encoding="utf-8")
    assert 'top_heavy: "Body sits in the top",' in panel and "top_heavy: AlignVerticalJustifyStart," in panel
    assert 'wrapped: "Title wraps to a second line",' in panel and "wrapped: WrapText," in panel


def test_a_five_series_legend_on_two_rows_in_a_half_width_panel_is_reported():
    spec = _deck(_content("Board", _CARD), _content("B"))
    observation = {"kind": "wrap_legend", "rows": 2, "series": 5, "panel": 2, "chart": 2, "text": "Installed base"}
    (finding,) = findings_from_observations([_slide(0, [observation])], spec)
    assert finding.kind == "wrap_legend"
    assert finding.severity == "advisory"
    assert finding.amount == 2
    assert finding.message == (
        'the chart legend in panel 2 "Installed base" wraps its 5 entries onto 2 rows - a legend reads on one. '
        "Shorten the series names, merge the smallest series into one, or widen the panel "
        "(a larger span, or fewer panel_columns)."
    )
    # The same chart with short names: its legend holds on one row.
    assert findings_from_observations([_slide(0, [{**observation, "rows": 1}])], spec) == []


def test_a_wrapped_legend_outside_a_panel_can_move_to_the_right():
    spec = _deck(_content("Board", _CARD), _content("B"))
    observation = {"kind": "wrap_legend", "rows": 3, "series": 6, "panel": 0, "chart": 1, "text": ""}
    (finding,) = findings_from_observations([_slide(0, [observation])], spec)
    assert finding.message.startswith("the chart legend in chart 1 wraps its 6 entries onto 3 rows")
    assert finding.message.endswith("give the chart a wider column, or set legend_position: right.")


@pytest.mark.parametrize(
    "owner, panel, expected",
    [
        (
            "panel",
            3,
            'the panel title in panel 3 "Installed base by version and region" wraps to 2 lines - a title reads '
            "on one. Shorten it: drop a qualifier the slide title already carries (the period, the unit, the "
            "scope), or widen the panel (a larger span, or fewer panel_columns).",
        ),
        (
            "slide",
            0,
            'the slide title "Installed base by version and region" wraps to 2 lines - a title reads on one. '
            "Shorten it to the claim in its key words; a second idea is a second slide.",
        ),
    ],
)
def test_a_title_on_two_lines_is_reported_wrap_title(owner, panel, expected):
    spec = _deck(_content("Board", _CARD), _content("B"))
    observation = {
        "kind": "wrap_title",
        "owner": owner,
        "lines": 2,
        "panel": panel,
        "chart": 0,
        "text": "Installed base by version and region",
    }
    (finding,) = findings_from_observations([_slide(0, [observation])], spec)
    assert (finding.kind, finding.severity, finding.amount) == ("wrap_title", "advisory", 2)
    assert finding.message == expected
    # One line is the design.
    assert findings_from_observations([_slide(0, [{**observation, "lines": 1}])], spec) == []


def test_wrap_title_and_wrap_legend_are_measured_by_the_audit_script():
    from app.api.v1.schemas.presentation import PresentationLayoutFinding
    from app.services.presentation_export import LAYOUT_AUDIT_JS

    assert 'findings.push({ kind: "wrap_title", owner, lines, panel, chart, text: excerpt(title.textContent) });' in (
        LAYOUT_AUDIT_JS
    )
    assert 'findings.push({ kind: "wrap_legend", rows, series: entries.length, panel, chart, text: name });' in (
        LAYOUT_AUDIT_JS
    )
    # A legend placed right is a column by design and never counted.
    assert 'figure.dataset.legendPosition === "right"' in LAYOUT_AUDIT_JS
    assert {"wrap_title", "wrap_legend"} <= set(get_args(layout.FindingKind))
    description = PresentationLayoutFinding.model_fields["kind"].description or ""
    assert "wrap_title" in description and "wrap_legend" in description
    panel = (PRESENTATION_DIR / "PresentationLayoutPanel.tsx").read_text(encoding="utf-8")
    assert 'wrap_title: "Slide, panel or chart title wraps",' in panel and "wrap_title: WrapText," in panel
    assert 'wrap_legend: "Chart legend wraps",' in panel and "wrap_legend: WrapText," in panel


def test_the_presentation_skill_carries_the_one_line_rule():
    skill = (
        Path(__file__).resolve().parents[1] / "app" / "agents" / "seed_data" / "skill_content" / "presentation-design.md"
    ).read_text(encoding="utf-8")
    assert "**Titles and legends on one line.**" in skill
    review = skill.split("## 6. Review pass before you answer", 1)[1]
    assert "chart legend fits on one line" in review and "wrap_legend" in review


def test_an_observation_without_balance_facts_grades_as_before():
    """An observation from a render that predates the balance facts (no
    `room`) yields the sparse finding alone, unchanged."""
    spec = _deck(_content("Poll", _CARD), _content("B"))
    assert [f.kind for f in findings_from_observations([_slide(0, [_SPARSE_BODY])], spec)] == ["sparse"]


def test_a_dense_slide_is_never_also_uneven_or_floating():
    spec = _deck(_columns("KPIs", [_KPI], [_CARD]), _content("Poll", _CARD))
    fit = {"kind": "fit", "scale": 0.8}
    observations = [
        _slide(0, [_balance(0.2, blocks=2, ends=[600, 150]), fit]),
        _slide(1, [_balance(0.62, blocks=1), fit]),
    ]
    assert [f.kind for f in findings_from_observations(observations, spec)] == ["fit", "fit"]


def test_the_balance_facts_are_measured_by_the_audit_script():
    """The script reports, on the sparse observation, the room's height, the
    count of blocks it drew and each column's bottom edge from the room's
    top; the panel labels both kinds and the API documents them."""
    from app.api.v1.schemas.presentation import PresentationLayoutFinding
    from app.services.presentation_export import LAYOUT_AUDIT_JS

    assert "room: Math.round(rr.height), blocks: drawn, ends });" in LAYOUT_AUDIT_JS
    assert 'host.querySelectorAll(":scope > .deck-column")' in LAYOUT_AUDIT_JS
    assert "ends.push(end === null ? null : Math.round(end - rr.top));" in LAYOUT_AUDIT_JS
    assert {"uneven", "floating"} <= set(get_args(layout.FindingKind))
    description = PresentationLayoutFinding.model_fields["kind"].description or ""
    assert "uneven" in description and "floating" in description
    panel = (PRESENTATION_DIR / "PresentationLayoutPanel.tsx").read_text(encoding="utf-8")
    assert 'uneven: "Columns end apart",' in panel and "uneven: Columns2," in panel
    assert 'floating: "Lone block floats at the top",' in panel and "floating: AlignVerticalJustifyStart," in panel


def _values(**overrides) -> dict:
    """A ``values`` observation as ``LAYOUT_AUDIT_JS`` reads it off a chart's facts."""
    return {
        "kind": "values",
        "block": "chart",
        "chart": 2,
        "chart_kind": "bar",
        "series": 6,
        "categories": 16,
        "asked": 96,
        "written": 0,
        "reason": "narrow",
        "room": 12,
        "need": 17,
        **overrides,
    }


def test_a_chart_that_cannot_write_its_values_is_told_which_chart_and_why():
    """Values the block asked for and the figure could not write are a
    finding - "values hidden on chart N" with the shape, the room it had and
    the room it needed - never a silent drop. Dense: a board deck without its
    numbers is a slide the audience reads with difficulty."""
    (finding,) = findings_from_observations([_slide(0, [_values()])], _spec(["ARR"]))
    assert finding.kind == "values"
    assert finding.severity == "dense"
    assert finding.amount == 96
    assert finding.message.startswith("values hidden on chart 2 (bar, 6 series x 16 categories): 96 of 96 values")
    assert "12px" in finding.message and "17px" in finding.message
    assert "fewer categories or series" in finding.message
    assert "show_values false" in finding.message
    # The report line the agent reads names the slide first.
    report = format_layout_report(LayoutReport(slide_count=1, findings=(finding,)))
    assert '- slide 1 [content] "ARR" (dense): values hidden on chart 2' in report


def test_a_maps_countries_beyond_its_region_are_named_with_the_remedy():
    """A map zoomed on a region writes every country it draws, inside
    it or on hover; the ones the region leaves out are hidden values, named by
    code, and the remedy is the world map or another map - never more room."""
    from app.services.presentation_export import LAYOUT_AUDIT_JS

    beyond = _values(
        chart=1, chart_kind="map", series=1, categories=6, asked=6, written=4,
        reason="region", region="europe", countries=["US", "AU"],
    )
    (finding,) = findings_from_observations([_slide(0, [beyond])], _spec(["Customers"]))
    assert finding.kind == "values" and finding.severity == "dense" and finding.amount == 2
    assert finding.message == (
        "values hidden on chart 1 (map, 1 series x 6 categories): 2 of 6 values are not written - 2 of its 6 "
        "countries (US, AU) lie outside region europe. Drop region to draw the whole world, or take these "
        "countries out of this map (a second map of their region, or a table)."
    )
    assert "more width" not in finding.message and "height" not in finding.message
    # The audit reads them off the map's facts.
    assert "region: facts.dataset.region" in LAYOUT_AUDIT_JS
    assert 'countries: (facts.dataset.valuesCountries || "").split(",")' in LAYOUT_AUDIT_JS
    map_canvas = (PRESENTATION_DIR / "blocks" / "MapCanvas.tsx").read_text(encoding="utf-8")
    assert 'const BEYOND_THE_REGION: HiddenValuesReason = "region";' in map_canvas
    assert '"data-values-reason": BEYOND_THE_REGION' in map_canvas and '"data-values-countries"' in map_canvas


def test_each_reason_for_hidden_values_names_its_own_remedy():
    short = _values(reason="short", room=150, need=190, chart=1, chart_kind="stacked_bar", written=4, asked=16)
    (finding,) = findings_from_observations([_slide(0, [short])], _spec(["A"]))
    assert finding.message.startswith("values hidden on chart 1 (stacked bar, 6 series x 16 categories): 12 of 16")
    assert "size md or lg, or fill" in finding.message
    dense = _values(reason="dense", chart_kind="pie", series=1, categories=30, room=24, need=30, asked=30)
    (finding,) = findings_from_observations([_slide(0, [dense])], _spec(["A"]))
    assert "at most 24 slices and this one has 30" in finding.message
    assert "Other" in finding.message
    # A combo line's value with a bar or a bar's value over and under its point and no room above.
    overlap = _values(reason="overlap", chart_kind="combo", series=3, categories=7, asked=21, written=20)
    (finding,) = findings_from_observations([_slide(0, [overlap])], _spec(["A"]))
    assert finding.severity == "dense" and finding.amount == 1
    assert "1 of 21 values are not written - a line's value finds a bar, a bar's value" in finding.message
    assert "axis secondary" in finding.message
    # The pinned ranges part a line from the bars, a series' own switch leaves its values to the tooltip.
    assert "y_max" in finding.message and "secondary_y_min" in finding.message
    assert "show_values false on one series" in finding.message


def test_a_line_too_narrow_for_its_values_is_told_they_were_tried_alternating_too():
    """A line's values are tried in one row and alternating above and
    below the points before any is hidden, so a line still reported `narrow`
    had no room either way - the finding says so, with the same remedies; a
    bar's wording is unchanged."""
    line = _values(chart_kind="line", series=1, categories=16, asked=16, written=0, room=22, need=62)
    (finding,) = findings_from_observations([_slide(0, [line])], _spec(["ASP"]))
    assert finding.kind == "values" and finding.severity == "dense"
    assert finding.message.startswith("values hidden on chart 2 (line, 1 series x 16 categories): 16 of 16 values are not written")
    assert "the run between two points is 22px and even the smallest label needs 62px" in finding.message
    assert "written alternately above and below the points" in finding.message
    assert "turned on its side" not in finding.message
    assert "fewer categories or series" in finding.message and "show_values false" in finding.message
    (area,) = findings_from_observations([_slide(0, [_values(chart_kind="area", series=1, categories=16)])], _spec(["A"]))
    assert "alternately above and below" in area.message
    (bars,) = findings_from_observations([_slide(0, [_values()])], _spec(["A"]))
    assert "turned on its side" in bars.message and "alternately" not in bars.message


def _thin(**overrides) -> dict:
    """A stack's segments still hidden once inside and past the stack were tried,
    as ``LAYOUT_AUDIT_JS`` reads them: the thinnest one's share, its
    length and its need, what the drawing's plan at the tallest canvas and
    across a full row writes, and the series they belong to."""
    facts = {
        "reason": "segments",
        "chart_kind": "stacked_bar",
        "series": 3,
        "categories": 8,
        "asked": 32,
        "written": 24,
        "room": 4,
        "need": 24,
        "share": 0.012,
        "reach": 6,
        "taller": False,
        "wider": False,
        "hidden_series": ["Partners"],
        "panel": False,
        "horizontal": False,
    }
    return _values(**{**facts, **overrides})


def _thin_message(**overrides) -> str:
    (finding,) = findings_from_observations([_slide(0, [_thin(**overrides)])], _spec(["A"]))
    assert finding.kind == "values" and finding.severity == "advisory"
    return finding.message


def test_segments_too_thin_for_their_value_are_an_advisory_the_totals_written():
    """A stack's thin segments that hold their value neither inside nor past
    the stack; its totals are written, so the slide reads - a hint, not a
    failure - and the finding says how thin the thinnest is."""
    (finding,) = findings_from_observations([_slide(0, [_thin()])], _spec(["A"]))
    assert finding.severity == "advisory"
    assert finding.amount == 8
    assert finding.message.startswith(
        "values hidden on chart 2 (stacked bar, 3 series x 8 categories): 8 of 32 values are not written - segments "
        "of the stacks are too thin to hold their value and the room past their stack is taken: the thinnest is "
        "1.2% of the axis range, 4px long, and its value needs 24px inside it."
    )
    assert "the stack totals are written" in finding.message


def test_more_height_is_said_not_to_write_thin_segments_when_the_tallest_canvas_would_not():
    """A segment that is 1-3% of its stack stays too thin at any height,
    and recommending size lg or fill sent the agent after room that changes
    nothing. When the drawing's own plan at the tallest canvas still hides the
    values, the finding says so - with the length the thinnest would reach -
    and points at the real options: the series' own show_values, or leaving it."""
    message = _thin_message()
    assert (
        "More height would not write them: even 640px high, the most a slide gives, the thinnest segment would be "
        "6px long and its value needs 24px."
    ) in message
    assert 'Set show_values false on series "Partners" to leave its values to the tooltip, or leave it' in message
    assert "size lg" not in message and "fill" not in message
    assert "Give the chart more" not in message


@pytest.mark.parametrize(
    "facts",
    [
        {},
        {"wider": True},
        {"panel": True},
        {"horizontal": True},
        {"horizontal": True, "wider": True},
        {"need": 0, "share": 0},
    ],
    ids=["neither", "wider", "panel", "horizontal", "horizontal_wider", "no_lengths"],
)
def test_the_layout_check_never_recommends_size_lg_or_fill_when_it_would_not_write_them(facts):
    """The acceptance criterion, whatever else is true of the chart."""
    message = _thin_message(**facts)
    assert "size lg" not in message
    assert "fill" not in message
    assert "Give the chart more height" not in message and "Give its panel more height" not in message


def test_more_height_is_recommended_only_where_the_tallest_canvas_writes_every_value():
    message = _thin_message(taller=True)
    assert (
        "Give the chart more height (size lg, or fill with at least 640px): every value is written at 640px. "
        'Otherwise set show_values false on series "Partners"'
    ) in message
    assert "would not write them" not in message
    # In a dashboard panel, size and fill are not the chart's own: its panel takes the height.
    panel = _thin_message(taller=True, panel=True)
    assert "Give its panel more height (fewer key figures or panel rows on the slide)" in panel
    assert "size lg" not in panel and "fill" not in panel


def test_more_width_is_recommended_only_where_a_full_row_writes_every_value():
    message = _thin_message(wider=True, hidden_series=["Marketplace", "Partners", "Resellers"])
    assert "More height would not write them" in message
    assert "Give the chart more width (a full-width row instead of a column): every value is written there." in message
    assert 'Otherwise set show_values false on series "Marketplace", "Partners" and "Resellers" to leave their values' in message
    # On its side a stack's segments lengthen with width: that is what it says it lacks.
    sideways = _thin_message(horizontal=True, taller=True, reach=9, need=76)
    assert "More width would not write them: even across a full row, the thinnest segment would be 9px long" in sideways
    assert "Give the chart more height (size lg, or fill with at least 640px)" in sideways
    assert "Give the chart more width" not in sideways


def test_a_chart_too_short_to_read_is_not_also_told_its_values_lack_the_height():
    """A chart drawn under ``CHART_MIN_HEIGHT_PX`` is told to take more
    height; its values wanting that same height is the same remedy, said once.
    A chart too NARROW for its values is a different remedy and is kept."""
    too_short = {"kind": "chart", "height": 120, "chart": "bar", "fill": False, "panel": False, "block": "chart"}
    short = _values(reason="short", room=70, need=150)
    narrow = _values(chart=3)
    findings = findings_from_observations([_slide(0, [too_short, short, narrow])], _spec(["A"]))
    assert sorted(f.kind for f in findings) == ["chart", "values"]
    (values,) = [f for f in findings if f.kind == "values"]
    assert values.message.startswith("values hidden on chart 3 ")
    # Without the short chart, the short values stand on their own.
    (finding,) = findings_from_observations([_slide(0, [short])], _spec(["A"]))
    assert finding.kind == "values"


@pytest.mark.parametrize(
    "observation",
    [
        _values(written=96),
        _values(asked=0, written=0),
        _values(written=120),
        _values(asked="x"),
    ],
)
def test_a_chart_that_writes_every_value_it_was_asked_for_is_not_a_finding(observation):
    assert findings_from_observations([_slide(0, [observation])], _spec(["A"])) == []


def test_the_audit_reads_the_values_a_chart_was_asked_for_off_its_facts():
    """The page names a chart by its position among the slide's charts and
    reads what its drawing counted on ``.deck-chart-facts`` (``ChartCanvas.tsx``
    publishes the counts ``chart-layout.ts`` decided)."""
    from app.services.presentation_export import LAYOUT_AUDIT_JS

    for needle in (
        'slide.querySelectorAll(".deck-chart")',
        'figure.querySelector(".deck-chart-facts")',
        "Number(facts.dataset.valuesAsked)",
        "Number(facts.dataset.valuesWritten)",
        "if (!(asked > written) && !rotated && !shrunk) return;",
        'block: "chart", chart: position + 1, chart_kind: figure.dataset.kind || ""',
        'kind: "values", ...chart, asked, written',
        'reason: facts.dataset.valuesReason || ""',
        # What more room would do for a stack's thin segments, and whose they are.
        "share: Number(facts.dataset.valuesShare) || 0, reach: Number(facts.dataset.valuesReach) || 0",
        'taller: facts.dataset.valuesTaller === "true", wider: facts.dataset.valuesWider === "true"',
        'hidden_series: hiddenSeries, horizontal: shape.horizontal === true, panel: !!figure.closest(".deck-panel")',
        # The fit the values were drawn under: which room was tried.
        'fit: facts.dataset.valuesFit || ""',
    ):
        assert needle in LAYOUT_AUDIT_JS, needle
    canvas = (PRESENTATION_DIR / "blocks" / "ChartCanvas.tsx").read_text(encoding="utf-8")
    for needle in (
        "data-values-asked={values.asked}",
        "data-values-written={values.written}",
        '"data-values-reason": values.reason',
        '"data-values-room": values.room',
        '"data-values-need": values.need',
        '"data-values-reach": thin.reach',
        '"data-values-taller": thin.taller ? "true" : "false"',
        '"data-values-wider": thin.wider ? "true" : "false"',
        '"data-values-series": thin.series.join(",")',
    ):
        assert needle in canvas, needle
    # The reasons the drawing publishes are the ones the audit has words for.
    chart_layout = (PRESENTATION_DIR / "blocks" / "chart-layout.ts").read_text(encoding="utf-8")
    union = re.search(r"export type HiddenValuesReason = ([^;]+);", chart_layout)
    assert union
    assert set(re.findall(r'"(\w+)"', union.group(1))) == set(layout._VALUES_REMEDIES)


def _rotated(**overrides) -> dict:
    return {
        "kind": "rotated",
        "block": "chart",
        "chart": 1,
        "chart_kind": "bar",
        "series": 3,
        "categories": 8,
        "label_size": "md",
        "size": 22,
        "upright": 12,
        "room": 25,
        "need": 42,
        **overrides,
    }


def test_values_turned_on_their_side_are_an_advisory_naming_the_room_they_lacked():
    """Every value is written, but turned on its side - a board deck
    compared with the dashboard card it replaces reads them upright. Advisory,
    with the smallest size they were tried upright in, the room between two
    bars and what the widest needed, and the remedies the agent has."""
    (finding,) = findings_from_observations([_slide(0, [_rotated()])], _spec(["Sources"]))
    assert finding.kind == "rotated"
    assert finding.severity == "advisory"
    assert finding.amount == 22
    assert finding.message == (
        "values rotated on chart 1 (bar, 3 series x 8 categories): the values are turned on their side at 22px - "
        "set upright at 12px, the widest needs 42px across and the bars stand 25px apart. Set value_label_fit "
        "stagger to keep them upright at this text size on two alternating rows, set label_size sm for "
        "smaller values and axis text, give the chart more width (a full-width row instead of a column, or a wider "
        "column), or use fewer categories or series."
    )
    report = format_layout_report(LayoutReport(slide_count=1, findings=(finding,)))
    assert '- slide 1 [content] "Sources" (advisory): values rotated on chart 1' in report


def test_a_chart_already_at_the_smallest_label_size_is_not_told_to_shrink_it():
    (finding,) = findings_from_observations([_slide(0, [_rotated(label_size="sm", upright=10)])], _spec(["A"]))
    assert "set upright at 10px" in finding.message
    assert "label_size" not in finding.message
    assert finding.message.endswith(
        "Set value_label_fit stagger to keep them upright at this text size on two alternating rows, give the "
        "chart more width (a full-width row instead of a column, or a wider column), or use fewer categories or series."
    )
    (lg,) = findings_from_observations([_slide(0, [_rotated(label_size="lg")])], _spec(["A"]))
    assert "set label_size md" in lg.message


def test_turned_values_with_a_size_of_their_own_are_told_to_shrink_it_not_label_size():
    """Values sized by ``value_label_size`` do not follow ``label_size``,
    so the remedy names the value size - one step down, the axis text left
    alone - and only while a smaller one tries them under the smallest size
    they were tried upright in."""
    (number,) = findings_from_observations([_slide(0, [_rotated(value_label_size=20, upright=12)])], _spec(["A"]))
    assert "set value_label_size 18 for smaller values, the axis text unchanged" in number.message
    assert "label_size sm" not in number.message
    (word,) = findings_from_observations([_slide(0, [_rotated(value_label_size="md", upright=12)])], _spec(["A"]))
    assert "set value_label_size sm for smaller values, the axis text unchanged" in word.message
    # Already at the floor: 16 tries 16 down to 10, sm 18 down to 10, 11 only 11 - none goes lower.
    for floor, upright in ((16, 10), ("sm", 10), (11, 11)):
        (finding,) = findings_from_observations(
            [_slide(0, [_rotated(value_label_size=floor, upright=upright)])], _spec(["A"])
        )
        assert "value_label_size" not in finding.message and "label_size" not in finding.message, floor
        assert finding.message.endswith(
            "give the chart more width (a full-width row instead of a column, or a wider column), or use fewer "
            "categories or series."
        )
    # A chart that names none is told what it always was.
    (plain,) = findings_from_observations([_slide(0, [_rotated(value_label_size=None)])], _spec(["A"]))
    assert "set label_size sm for smaller values and axis text" in plain.message


def test_values_left_out_to_stay_upright_at_their_own_size_are_told_to_shrink_it():
    """``value_label_fit`` upright keeps the values' first size; when
    ``value_label_size`` sets it, a smaller ``label_size`` would change nothing."""
    observation = _values(chart=1, reason="upright", asked=21, written=18, room=43, need=59, value_label_size=20)
    (finding,) = findings_from_observations([_slide(0, [observation])], _spec(["A"]))
    assert finding.message.endswith(layout._UPRIGHT_REMEDY_OWN_VALUE_SIZE)
    assert "a smaller value_label_size" in finding.message
    assert "a smaller label_size" not in finding.message


def test_the_audit_reads_the_values_own_size_off_the_payload():
    from app.services.presentation_export import LAYOUT_AUDIT_JS

    assert "value_label_size: shape.value_label_size ?? null," in LAYOUT_AUDIT_JS


def test_turned_values_are_offered_a_stagger_only_on_the_charts_it_acts_on():
    """``value_label_fit`` stagger keeps the values upright at their
    size on a bar, stacked bar or combo chart; a waterfall turns its values
    too, and is not offered a field that does nothing there."""
    for kind in ("bar", "stacked_bar", "combo"):
        (finding,) = findings_from_observations([_slide(0, [_rotated(chart_kind=kind)])], _spec(["A"]))
        assert "Set value_label_fit stagger" in finding.message, kind
    (waterfall,) = findings_from_observations([_slide(0, [_rotated(chart_kind="waterfall")])], _spec(["A"]))
    assert "value_label_fit" not in waterfall.message
    assert "Set label_size sm" in waterfall.message


def test_values_left_out_to_stay_upright_name_the_room_and_offer_the_stagger():
    """``value_label_fit`` upright keeps the size asked on one row and
    leaves out a value that would touch another - reason ``upright``, with the
    room between two bars once they gave way and what the widest needs."""
    observation = _values(chart=1, reason="upright", asked=21, written=18, room=43, need=59)
    (finding,) = findings_from_observations([_slide(0, [observation])], _spec(["A"]))
    assert finding.kind == "values"
    assert finding.severity == "dense"
    assert finding.amount == 3
    assert "3 of 21 values are not written - kept upright at the size asked (value_label_fit upright)" in finding.message
    assert "the widest needs 59px across and the bars stand 43px apart" in finding.message
    assert finding.message.endswith(layout._VALUES_REMEDIES["upright"])
    assert "Set value_label_fit stagger" in finding.message


def test_a_horizontal_chart_is_told_its_rows_lack_height_and_its_values_past_the_bars_width():
    """A horizontal chart's rows share the plot's height and its values
    run along them to the right edge. Values with no room across the rows
    (``narrow``) are offered ``value_label_fit`` stagger and more height, never
    more width; values with no room past the bars (``short``) more width."""
    narrow = _values(chart_kind="bar", horizontal=True, reason="narrow", room=13, need=18, asked=16, written=0)
    (finding,) = findings_from_observations([_slide(0, [narrow])], _spec(["A"]))
    assert "16 of 16 values are not written - the rows stand 13px apart and even the smallest value needs 18px across them" in finding.message
    assert finding.message.endswith(layout._HORIZONTAL_VALUES_REMEDIES["narrow"])
    assert "Set value_label_fit stagger" in finding.message and "more height" in finding.message
    assert "more width" not in finding.message and "turned on its side" not in finding.message
    short = _values(chart_kind="stacked_bar", horizontal=True, reason="short", room=554, need=620)
    (finding,) = findings_from_observations([_slide(0, [short])], _spec(["A"]))
    assert "the plot is 554px wide and needs 620px" in finding.message
    assert finding.message.endswith(layout._HORIZONTAL_VALUES_REMEDIES["short"])
    assert "more height" not in finding.message
    # A combo is never drawn on its side, whatever the payload says: upright wording.
    combo = _values(chart_kind="combo", horizontal=True, reason="narrow")
    (finding,) = findings_from_observations([_slide(0, [combo])], _spec(["A"]))
    assert finding.message.endswith(layout._VALUES_REMEDIES["narrow"])


def test_a_butterfly_is_told_its_rows_lack_height_or_its_two_sides_width_never_a_bar_chart_s_switch():
    """A butterfly's rows share the plot's height and its values run out
    from the spine to both edges. Rows too close (``narrow``) are offered more
    height; sides too narrow for the values past their bars (``short``) more
    width or the spine's column - never ``value_label_fit``, which it ignores."""
    shape = {"chart": 1, "series": 2, "categories": 7}
    narrow = _values(chart_kind="butterfly", horizontal=True, reason="narrow", room=14, need=18, asked=14, written=0, **shape)
    (finding,) = findings_from_observations([_slide(0, [narrow])], _spec(["A"]))
    assert "values hidden on chart 1 (butterfly, 2 series x 7 categories)" in finding.message
    assert "14 of 14 values are not written - the rows stand 14px apart and even the smallest value needs 18px across them" in finding.message
    assert finding.message.endswith(layout._BUTTERFLY_VALUES_REMEDIES["narrow"])
    short = _values(chart_kind="butterfly", horizontal=True, reason="short", room=61, need=96, asked=14, written=0, **shape)
    (finding,) = findings_from_observations([_slide(0, [short])], _spec(["A"]))
    assert "its two sides keep 61px for their bars once the values past them take their room, and need 96px" in finding.message
    assert finding.message.endswith(layout._BUTTERFLY_VALUES_REMEDIES["short"])
    assert "spine left" in finding.message
    for message in (layout._BUTTERFLY_VALUES_REMEDIES["narrow"], layout._BUTTERFLY_VALUES_REMEDIES["short"]):
        assert "value_label_fit" not in message


def test_horizontal_values_left_out_to_stay_upright_name_the_rows_and_offer_the_stagger():
    """``value_label_fit`` upright on a horizontal chart keeps the size
    asked in place and leaves out a value that would touch the one in the next
    row - the room is the pitch of two rows, the need a value's height."""
    observation = _values(chart_kind="bar", horizontal=True, reason="upright", asked=16, written=8, room=13, need=23)
    (finding,) = findings_from_observations([_slide(0, [observation])], _spec(["A"]))
    assert finding.severity == "dense"
    assert (
        "8 of 16 values are not written - kept at the size asked (value_label_fit upright), a value needs 23px "
        "across the rows and they stand 13px apart even once the bars gave way"
    ) in finding.message
    assert finding.message.endswith(layout._HORIZONTAL_VALUES_REMEDIES["upright"])
    assert "moved along its row" in finding.message and "two alternating rows" not in finding.message


def test_segments_a_stack_kept_upright_are_offered_the_stagger_first():
    """A horizontal stack fitted ``upright`` keeps the size asked and
    leaves out a segment's value too long for its segment - the room past the
    stack is not tried, so the finding says so and offers ``stagger``, which
    writes it there; under ``stagger`` that room was tried and is taken."""
    upright = _thin_message(fit="upright", horizontal=True, wider=True)
    assert "kept at the size asked (value_label_fit upright), segments of the stacks are too thin to hold their value:" in upright
    assert "room past their stack is taken" not in upright
    assert ". Set value_label_fit stagger to write them past their stack, with a leader, at this size." in upright
    assert "Give the chart more width (a full-width row instead of a column): every value is written there." in upright
    staggered = _thin_message(fit="stagger", horizontal=True)
    assert "kept at the size asked (value_label_fit stagger), segments of the stacks are too thin to hold their value and the room past their stack is taken" in staggered
    assert "Set value_label_fit stagger" not in staggered
    # Without a fit, the wording is unchanged.
    assert "Set value_label_fit stagger" not in _thin_message()


def test_values_turned_for_another_neighbour_than_the_next_bar_say_so():
    """The widest value may fit between two bars and still touch a taller bar
    or another value; the finding never states a room larger than the need."""
    (finding,) = findings_from_observations([_slide(0, [_rotated(room=40, need=36)])], _spec(["A"]))
    assert "a value would touch the value or the bar beside it" in finding.message
    assert "40px" not in finding.message


def test_a_rotated_chart_with_hidden_values_is_told_both():
    """The two findings have different remedies: both are kept, the hidden values first."""
    findings = findings_from_observations([_slide(0, [_values(chart=1), _rotated()])], _spec(["A"]))
    assert [f.kind for f in findings] == ["values", "rotated"]


def test_the_audit_reads_the_turned_values_off_the_facts():
    """The drawing publishes the turn and the room it lacked upright
    (``ValuePlan.turned``); the audit reads them with the chart's ``label_size``."""
    from app.services.presentation_export import LAYOUT_AUDIT_JS

    for needle in (
        'const rotated = facts.dataset.labelRotate === "true";',
        'kind: "rotated", ...chart, label_size: figure.dataset.labelSize || "md"',
        "size: Number(facts.dataset.labelSize) || 0, upright: Number(facts.dataset.uprightSize) || 0",
        "room: Number(facts.dataset.uprightRoom) || 0, need: Number(facts.dataset.uprightNeed) || 0",
    ):
        assert needle in LAYOUT_AUDIT_JS, needle
    canvas = (PRESENTATION_DIR / "blocks" / "ChartCanvas.tsx").read_text(encoding="utf-8")
    assert (
        '{ "data-upright-size": turned.size, "data-upright-room": turned.room, "data-upright-need": turned.need }'
        in canvas
    )
    figure = (PRESENTATION_DIR / "blocks" / "chart.tsx").read_text(encoding="utf-8")
    assert 'data-label-size={block.label_size ?? "md"}' in figure
    # The remedy names a `label_size` the spec accepts, one step down.
    assert set(layout._SMALLER_LABEL_SIZE) | set(layout._SMALLER_LABEL_SIZE.values()) == set(get_args(Size))


def _shrunk(**overrides) -> dict:
    """A pie's values set under the size asked, as ``LAYOUT_AUDIT_JS``
    reads them: the size they are set at, the size asked, the radius the disc
    would keep with them outside at it and the least it keeps."""
    return {
        "kind": "shrunk",
        "block": "chart",
        "chart": 1,
        "chart_kind": "pie",
        "series": 1,
        "categories": 2,
        "label_size": "md",
        "size": 14,
        "asked_size": 22,
        "room": 29,
        "need": 53,
        "placement": "outside",
        "legend_position": "right",
        **overrides,
    }


def test_a_pie_whose_values_are_set_smaller_is_an_advisory_naming_the_radius_they_would_take():
    """Every value is on the figure, smaller than ``label_size`` sets it -
    the ring they take outside the disc at that size would leave it under the
    radius it keeps. Advisory, with the room the legend beside it takes first."""
    (finding,) = findings_from_observations([_slide(0, [_shrunk()])], _spec(["Revenue mix"]))
    assert finding.kind == "shrunk"
    assert finding.severity == "advisory"
    assert finding.amount == 14
    assert finding.message == (
        "values shrunk on chart 1 (pie, 1 series x 2 categories): the values are set at 14px outside the pie, under "
        "the 22px of label_size md - at 22px the values outside it would leave the pie a 29px radius, under the 53px "
        "it keeps. Move the legend under the chart (legend_position bottom) to give the pie its width, give the "
        "chart more width (a full-width row instead of a column, or a wider column), or set label_size sm to make "
        "smaller values the chart's own."
    )
    report = format_layout_report(LayoutReport(slide_count=1, findings=(finding,)))
    assert '- slide 1 [content] "Revenue mix" (advisory): values shrunk on chart 1' in report


def test_pie_values_shrunk_to_part_two_of_them_say_so_and_a_legend_already_under_it_stays():
    observation = _shrunk(label_size="sm", size=12, asked_size=18, room=60, need=53, legend_position="bottom")
    (finding,) = findings_from_observations([_slide(0, [observation])], _spec(["A"]))
    assert "the values are set at 12px outside the pie, under the 18px of label_size sm" in finding.message
    assert "at 18px two values outside the pie would touch" in finding.message
    assert "legend_position" not in finding.message
    # Already the smallest text scale: no smaller label_size to offer.
    assert "label_size sm to" not in finding.message
    assert finding.message.endswith(
        "Give the chart more width (a full-width row instead of a column, or a wider column)."
    )


def test_pie_values_written_inside_the_slices_say_why():
    observation = _shrunk(chart_kind="donut", placement="inside", size=12, room=0, need=24)
    (finding,) = findings_from_observations([_slide(0, [observation])], _spec(["A"]))
    assert finding.message.startswith(
        "values shrunk on chart 1 (donut, 1 series x 2 categories): the values are set at 12px inside the slices, "
        "under the 22px of label_size md - at 22px the values outside it would leave the donut a 0px radius, under "
        "the 24px it keeps, and even the smallest size has no room outside it."
    )


@pytest.mark.parametrize("observation", [_shrunk(size=22), _shrunk(size=0), _shrunk(asked_size="x")])
def test_values_at_the_size_asked_are_not_shrunk(observation):
    assert findings_from_observations([_slide(0, [observation])], _spec(["A"])) == []


def test_a_pie_hiding_values_names_its_smallest_slice_and_the_remedies_a_pie_has():
    """A pie's values are tried outside the disc at every size, then
    inside the slices; what is still left out is a small slice's. The finding
    names how small, the room it had and what it needed - never a bar's room."""
    outside = _values(
        chart=1, chart_kind="pie", series=1, categories=6, asked=6, written=5, room=22, need=37, share=0.05,
        placement="outside", legend_position="right",
    )
    (finding,) = findings_from_observations([_slide(0, [outside])], _spec(["Regions"]))
    assert finding.kind == "values" and finding.severity == "dense" and finding.amount == 1
    assert finding.message == (
        "values hidden on chart 1 (pie, 1 series x 6 categories): 1 of 6 values are not written - the smallest of "
        "them is 5.0% of the pie, 22px along the rim where its value would be written outside it, and the value "
        "needs 37px there even at the smallest size, between its neighbours' values. Move the legend under the "
        "chart (legend_position bottom) to give the pie its width, give the chart more width (a full-width row "
        "instead of a column, or a wider column), group the smallest slices into one (Other), or set show_values "
        "false to leave the numbers to the tooltip."
    )
    inside = {**outside, "chart_kind": "donut", "categories": 2, "asked": 2, "written": 1, "share": 0.341}
    inside.update(placement="inside", room=47, need=72, legend_position="bottom")
    (finding,) = findings_from_observations([_slide(0, [inside])], _spec(["Revenue"]))
    assert (
        "1 of 2 values are not written - with no room outside the donut even at the smallest size, its values are "
        "written inside the slices that hold them, and the smallest of them is 34.1% of the donut: its value would "
        "need a disc of 72px in radius where this one is 47px."
    ) in finding.message
    assert "legend_position" not in finding.message
    assert "turned on its side" not in finding.message


def test_the_audit_reads_a_pies_shrunk_values_and_placement_off_the_facts():
    """The drawing publishes where a pie's values went and, set under the size
    asked, the size and the radius they would have taken (``chart-slices.ts``);
    the audit reads them with the chart's legend position."""
    from app.services.presentation_export import LAYOUT_AUDIT_JS

    for needle in (
        "const shrunk = facts.dataset.shrunkSize !== undefined;",
        'kind: "shrunk", ...chart, label_size: figure.dataset.labelSize || "md"',
        "size: Number(facts.dataset.labelSize) || 0, asked_size: Number(facts.dataset.shrunkSize) || 0",
        "room: Number(facts.dataset.shrunkRoom) || 0, need: Number(facts.dataset.shrunkNeed) || 0",
        "placement: facts.dataset.valuesPlacement || null,",
        "legend_position: figure.dataset.legendPosition || null,",
    ):
        assert needle in LAYOUT_AUDIT_JS, needle
    canvas = (PRESENTATION_DIR / "blocks" / "ChartCanvas.tsx").read_text(encoding="utf-8")
    for needle in (
        '{ "data-values-placement": pie.placement }',
        '"data-values-share": Math.round(pie.hiddenShare * 10_000) / 10_000',
        '{ "data-shrunk-size": shrunk.size, "data-shrunk-room": shrunk.room, "data-shrunk-need": shrunk.need }',
    ):
        assert needle in canvas, needle
    figure = (PRESENTATION_DIR / "blocks" / "chart.tsx").read_text(encoding="utf-8")
    # The block's own word - or a classed map's default, right - while a legend is drawn.
    assert "const placed = legendDrawn && (block.legend_position || position !== \"top\") ? position : undefined;" in figure
    assert "data-legend-position={placed}" in figure


def test_findings_are_sorted_worst_first_then_in_deck_order():
    observations = [
        _slide(3, [{"kind": "fit", "scale": 0.88}]),
        _slide(1, [{"kind": "clipped", "amount": 80, "block": "table-wrap", "text": "x"}]),
        _slide(0, [{"kind": "nowrap", "amount": 10, "block": "badge", "text": "y"}]),
        _slide(2, [{"kind": "fit", "scale": 0.6}]),
        _slide(4, [_SPARSE_BODY]),
    ]
    findings = findings_from_observations(observations, _spec(["A", "B", "C", "D", "E"]))
    assert [(f.severity, f.slide_index) for f in findings] == [
        ("overloaded", 1),
        ("overloaded", 2),
        ("dense", 0),
        ("dense", 3),
        ("advisory", 4),
    ]


def test_finding_and_report_serialize_for_the_api():
    finding = LayoutFinding(
        slide_index=4,
        slide_id="abc",
        layout="content",
        title="Roadmap",
        kind="fit",
        severity="dense",
        message="m",
        scale=0.9,
        amount=11,
    )
    report = LayoutReport(slide_count=6, findings=(finding,), unit="page", revision=7)
    payload = report.as_dict()
    # The API schema is the contract the frontend types mirror.
    from app.api.v1.schemas.presentation import PresentationLayoutCheckResponse

    parsed = PresentationLayoutCheckResponse.model_validate(payload)
    assert parsed.slide_count == 6
    assert parsed.skipped is None
    # The noun the UI speaks and the revision the panel marks stale against.
    assert parsed.unit == "page" and parsed.revision == 7
    (item,) = parsed.findings
    # Humans and the agent count slides from 1; the index stays for the UI.
    assert item.slide_index == 4 and item.slide_number == 5
    assert item.kind == "fit" and item.severity == "dense"
    assert item.scale == 0.9 and item.amount == 11
    assert not report.clean
    assert LayoutReport(slide_count=1).clean
    assert not LayoutReport(slide_count=0, skipped="disabled").clean
    # Defaults for a payload that predates the two fields.
    older = PresentationLayoutCheckResponse.model_validate({"slide_count": 1, "skipped": None, "findings": []})
    assert older.unit == "slide" and older.revision is None
    # The cache stores ``as_dict`` and reads it back as the same report; the
    # derived ``slide_number`` is dropped on the way in, not fed to the dataclass.
    assert LayoutReport.from_dict(json.loads(json.dumps(payload))) == report
    assert LayoutReport.from_dict({"slide_count": 2, "skipped": "x"}) == LayoutReport(slide_count=2, skipped="x")


def _held(**overrides) -> dict:
    """A ``size`` observation: a held block its host had to let go of."""
    return {"kind": "size", "block": "metrics_grid", "size": "md", "scale": 0.42, **overrides}


def test_a_block_drawn_below_its_size_is_told_it_no_longer_matches_the_deck():
    """``fit.ts`` holds a ``metrics_grid`` at its size while the rest of the
    body shrinks; a body that would still overflow at the floor lets go, and
    the grid is drawn at the body's ratio - smaller than the same size anywhere
    else in the deck. The finding says how small, why, and the remedies,
    one of which is the next smaller size on EVERY grid, so the rows match."""
    observations = [_slide(4, [{"kind": "fit", "scale": 0.42, "cut": 0}, _held()])]
    findings = findings_from_observations(observations, _spec(["A", "B", "C", "D", "Everything at once"]))
    (finding,) = [f for f in findings if f.kind == "size"]
    assert finding.severity == "dense"
    assert finding.scale == 0.42 and finding.amount == 42
    assert finding.message == (
        "the metrics_grid is drawn at 42% of size md: held at it, the rest of the slide would not fit even at "
        "the smallest text the slide allows, so it shrank with the body and reads smaller than a metrics_grid "
        "of size md anywhere else in the deck. Cut what shares the slide with it, move part of that content to "
        "a new slide, or set size sm on every metrics_grid of the deck."
    )
    # The body's own shrink is still reported, as before.
    assert any(f.kind == "fit" for f in findings)
    text = format_layout_report(LayoutReport(slide_count=5, findings=tuple(findings)))
    assert '- slide 5 [content] "Everything at once" (dense): the metrics_grid is drawn at 42% of size md' in text


def test_a_block_already_at_the_smallest_size_is_not_told_to_go_smaller_and_a_page_reads_page():
    (finding,) = findings_from_observations(
        [_slide(0, [_held(size="sm", scale=0.5)])], {"title": "Brief", "aspect_ratio": "A4", "slides": [{}]}
    )
    assert "set size" not in finding.message
    assert finding.message.endswith("Cut what shares the page with it or move part of that content to a new page.")
    lg = findings_from_observations([_slide(0, [_held(size="lg")])], _spec(["A"]))
    assert "set size md on every metrics_grid of the deck" in lg[0].message


@pytest.mark.parametrize("observation", [_held(scale=1), _held(scale=0), _held(scale="x"), _held(scale=None)])
def test_a_held_block_drawn_at_its_size_is_not_a_finding(observation):
    assert findings_from_observations([_slide(0, [observation])], _spec(["A"])) == []


def test_a_block_drawn_below_its_size_is_an_excess_and_silences_sparse():
    sparse = {"kind": "sparse", "unused": 0.6, "empty": 360, "slide": 1080}
    findings = findings_from_observations([_slide(0, [_held(), sparse])], _spec(["A"]))
    assert [f.kind for f in findings] == ["size"]


# ── The words the agent reads ────────────────────────────────────────────────


def test_a_disabled_check_says_nothing_at_all():
    assert format_layout_report(LayoutReport(slide_count=0, skipped="disabled")) == ""


def test_a_skipped_check_says_why_and_keeps_the_budget_in_view():
    note = format_layout_report(LayoutReport(slide_count=0, skipped="the render did not finish within 45 seconds"))
    assert note.startswith("Layout check skipped: the render did not finish within 45 seconds.")
    assert "density budget" in note


def test_a_clean_deck_is_told_so_in_one_line():
    assert (
        format_layout_report(LayoutReport(slide_count=12))
        == "Layout check: every one of the 12 slides fits its canvas."
    )


def test_findings_are_listed_by_slide_number_layout_and_title():
    findings = findings_from_observations(
        [
            _slide(2, [{"kind": "fit", "scale": 0.7}], layout_name="content"),
            _slide(0, [{"kind": "nowrap", "amount": 10, "block": "badge", "text": "y"}], layout_name="title"),
        ],
        _spec(["Opening", "B", "Dense one"]),
    )
    note = format_layout_report(LayoutReport(slide_count=3, findings=tuple(findings)))
    lines = note.splitlines()
    assert lines[0] == "Layout check: 2 slide issue(s) the audience would see. Fix them and save again:"
    # 1-based, as every validation error the agent already reads; worst first.
    assert lines[1].startswith('- slide 3 [content] "Dense one" (overloaded): the body needed 43% more height')
    assert lines[2].startswith('- slide 1 [title] "Opening" (dense): the badge "y" does not wrap')
    assert len(lines) == 3


def test_the_report_is_bounded_and_says_how_many_it_left_out():
    findings = tuple(
        LayoutFinding(
            slide_index=i,
            slide_id=None,
            layout=None,
            title="",
            kind="fit",
            severity="dense",
            message="m",
        )
        for i in range(MAX_REPORTED_FINDINGS + 3)
    )
    note = format_layout_report(LayoutReport(slide_count=20, findings=findings))
    lines = note.splitlines()
    assert len(lines) == 1 + MAX_REPORTED_FINDINGS + 1
    assert lines[-1] == "- ...and 3 more; fix the above, save, and the next check lists the rest."
    # No layout, no title: the slide number alone still locates it.
    assert lines[1] == "- slide 1 (dense): m"


# ── Running the check ────────────────────────────────────────────────────────


def _rendered(layout_observations: list[dict], slide_count: int = 3):
    from app.services.presentation_export import RenderedDeck

    return RenderedDeck(slide_count=slide_count, layout=layout_observations)


def test_a_disabled_check_never_touches_the_renderer(monkeypatch):
    from app.services import presentation_export

    async def _never(*a, **k):
        raise AssertionError("render_deck must not be called when the check is disabled")

    monkeypatch.setattr(presentation_export, "render_deck", _never)
    report = asyncio.run(check_presentation_layout(uuid.uuid4(), uuid.uuid4(), _spec(["A"])))
    assert report.skipped == "disabled"
    assert report.findings == ()


def test_the_render_runs_as_the_given_user_with_the_decks_paper_and_asks_for_layout(monkeypatch, layout_enabled):
    from app.services import presentation_export

    calls: list[tuple] = []

    async def _fake_render(presentation_id, user_id, **kwargs):
        calls.append((presentation_id, user_id, kwargs))
        return _rendered([_slide(1, [{"kind": "fit", "scale": 0.8}])])

    monkeypatch.setattr(presentation_export, "render_deck", _fake_render)
    deck_id, user_id = uuid.uuid4(), uuid.uuid4()
    spec = {**_spec(["A", "B", "C"]), "aspect_ratio": "A4"}
    report = asyncio.run(check_presentation_layout(deck_id, user_id, spec))

    ((called_deck, called_user, kwargs),) = calls
    assert (called_deck, called_user) == (deck_id, user_id)
    # A portrait page is measured on its own paper, not on the 16:9 canvas.
    assert kwargs == {"aspect_ratio": "A4", "want_layout": True}
    assert report.skipped is None
    assert report.slide_count == 3
    (finding,) = report.findings
    assert finding.slide_index == 1 and finding.title == "B"


def test_a_render_that_times_out_is_skipped_not_raised(monkeypatch, layout_enabled):
    from app.services import presentation_export

    async def _slow(*a, **k):
        await asyncio.sleep(10)

    monkeypatch.setattr(presentation_export, "render_deck", _slow)
    report = asyncio.run(check_presentation_layout(uuid.uuid4(), uuid.uuid4(), _spec(["A"]), timeout=0.01))
    assert report.skipped == "the render did not finish within 0 seconds"
    assert report.findings == ()


def test_a_render_error_is_reported_in_its_own_words(monkeypatch, layout_enabled):
    from app.services import presentation_export

    async def _broken(*a, **k):
        raise presentation_export.PresentationRenderError("The deck rendered no slides.")

    monkeypatch.setattr(presentation_export, "render_deck", _broken)
    report = asyncio.run(check_presentation_layout(uuid.uuid4(), uuid.uuid4(), _spec(["A"])))
    assert report.skipped == "The deck rendered no slides."


def test_a_missing_browser_is_skipped_with_a_deployment_message(monkeypatch, layout_enabled):
    from app.services import presentation_export

    async def _no_chromium(*a, **k):
        raise RuntimeError("Executable doesn't exist at .../chrome.exe")

    monkeypatch.setattr(presentation_export, "render_deck", _no_chromium)
    report = asyncio.run(check_presentation_layout(uuid.uuid4(), uuid.uuid4(), _spec(["A"])))
    assert report.skipped == "the headless browser is not available on this deployment"


# ── One check is about one revision ──────────────────────────────────────────


class _FakeRedis:
    """Enough of the client for the layout cache: ``get`` and ``set(..., ex=)``."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.expiries: list[int | None] = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.expiries.append(ex)


@pytest.fixture
def fake_redis(monkeypatch):
    redis = _FakeRedis()

    async def _client():
        return redis

    monkeypatch.setattr(layout, "redis_client", _client)
    return redis


def _render_counter(monkeypatch, *, revision: int | None, observations: list[dict] | None = None):
    """A fake ``render_deck`` that counts its calls and renders *revision*."""
    from app.services import presentation_export
    from app.services.presentation_export import RenderedDeck

    calls: list[dict] = []

    async def _fake_render(presentation_id, user_id, **kwargs):
        calls.append(kwargs)
        return RenderedDeck(
            slide_count=2,
            layout=observations if observations is not None else [_slide(1, [{"kind": "fit", "scale": 0.8}])],
            revision=revision,
        )

    monkeypatch.setattr(presentation_export, "render_deck", _fake_render)
    return calls


def test_a_save_landing_mid_render_is_a_skipped_check_not_a_misreport(monkeypatch, layout_enabled, fake_redis):
    """The render page loads the deck's CURRENT row. Asked about revision 1
    while a collaborator's save made it 2, the page rendered revision 2; its
    findings read against revision 1's titles would send the agent to fix the
    wrong slide. The check says what happened instead - and keeps nothing."""
    _render_counter(monkeypatch, revision=2)
    report = asyncio.run(check_presentation_layout(uuid.uuid4(), uuid.uuid4(), _spec(["A", "B"]), expected_revision=1))
    assert report.skipped == "the presentation changed while it was being measured (revision 1 -> 2); check again"
    assert report.findings == ()
    assert report.revision == 2
    assert "check again" in format_layout_report(report)
    assert fake_redis.store == {}, "a skipped check must not be served to the next caller"


def test_a_render_that_publishes_no_revision_is_trusted_as_the_one_asked_for(monkeypatch, layout_enabled, fake_redis):
    """A static page that predates ``data-deck-revision`` cannot be checked;
    the report is still produced and named after the revision requested."""
    _render_counter(monkeypatch, revision=None)
    report = asyncio.run(check_presentation_layout(uuid.uuid4(), uuid.uuid4(), _spec(["A", "B"]), expected_revision=4))
    assert report.skipped is None
    assert report.revision == 4
    assert len(report.findings) == 1


def test_a_report_is_kept_per_revision_and_served_without_a_second_render(monkeypatch, layout_enabled, fake_redis):
    """A Studio turn reads the deck back right after saving it, and the panel
    re-checks a deck nobody touched: the same revision is the same pixels. One
    render per (deck, user, revision); anything else renders again."""
    calls = _render_counter(monkeypatch, revision=3)
    deck, user, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    spec = _spec(["A", "B"])

    first = asyncio.run(check_presentation_layout(deck, user, spec, expected_revision=3))
    assert len(calls) == 1 and first.revision == 3 and len(first.findings) == 1
    assert fake_redis.expiries == [LAYOUT_CHECK_CACHE_TTL_SECS]
    assert list(fake_redis.store) == [f"presentation:layout:{deck}:{user}:3"]

    again = asyncio.run(check_presentation_layout(deck, user, spec, expected_revision=3))
    assert again == first
    assert len(calls) == 1, "the second check of the same revision must not render"

    # A new revision is new pixels; another user may see another deck (their
    # own template assets); a check with no revision cannot be matched at all.
    asyncio.run(check_presentation_layout(deck, other, spec, expected_revision=3))
    assert len(calls) == 2
    assert sorted(fake_redis.store) == sorted(
        [f"presentation:layout:{deck}:{user}:3", f"presentation:layout:{deck}:{other}:3"]
    )
    asyncio.run(check_presentation_layout(deck, user, spec))
    assert len(calls) == 3
    assert len(fake_redis.store) == 2, "an unpinned check is not stored"
    # Asked about revision 4 while the page still renders 3: a render, a
    # skipped report, and nothing kept under either revision.
    moved = asyncio.run(check_presentation_layout(deck, user, spec, expected_revision=4))
    assert len(calls) == 4
    assert moved.skipped is not None and moved.revision == 3
    assert len(fake_redis.store) == 2


def test_a_check_that_could_not_run_is_not_kept(monkeypatch, layout_enabled, fake_redis):
    from app.services import presentation_export

    async def _broken(*a, **k):
        raise presentation_export.PresentationRenderError("The deck rendered no slides.")

    monkeypatch.setattr(presentation_export, "render_deck", _broken)
    report = asyncio.run(check_presentation_layout(uuid.uuid4(), uuid.uuid4(), _spec(["A"]), expected_revision=1))
    assert report.skipped == "The deck rendered no slides."
    assert fake_redis.store == {}


def test_the_cache_is_a_courtesy_and_fails_open_to_a_render(monkeypatch, layout_enabled):
    """Redis down (or a corrupt entry) costs a render, never the check."""
    calls = _render_counter(monkeypatch, revision=1)

    async def _no_redis():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(layout, "redis_client", _no_redis)
    report = asyncio.run(check_presentation_layout(uuid.uuid4(), uuid.uuid4(), _spec(["A", "B"]), expected_revision=1))
    assert report.skipped is None and len(report.findings) == 1
    assert len(calls) == 1

    redis = _FakeRedis()
    deck, user = uuid.uuid4(), uuid.uuid4()
    redis.store[f"presentation:layout:{deck}:{user}:1"] = "not json {"

    async def _client():
        return redis

    monkeypatch.setattr(layout, "redis_client", _client)
    report = asyncio.run(check_presentation_layout(deck, user, _spec(["A", "B"]), expected_revision=1))
    assert report.skipped is None and len(report.findings) == 1
    assert len(calls) == 2
    # ...and the corrupt entry is replaced by the fresh report.
    assert json.loads(redis.store[f"presentation:layout:{deck}:{user}:1"])["revision"] == 1


def test_a_disabled_check_is_answered_before_the_cache_is_asked(monkeypatch):
    async def _never():
        raise AssertionError("redis must not be consulted when the check is disabled")

    monkeypatch.setattr(layout, "redis_client", _never)
    report = asyncio.run(check_presentation_layout(uuid.uuid4(), uuid.uuid4(), _spec(["A"]), expected_revision=1))
    assert report.skipped == "disabled"


# ── The tool result carries the check ────────────────────────────────────────


class _Result:
    def __init__(self, one=None, rows=None):
        self._one = one
        self._rows = rows or []

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._one


class _FakeSession:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.added: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, *a, **k):
        return self._results.pop(0) if self._results else _Result()

    async def commit(self):
        self.committed = True

    async def flush(self):
        return None

    def add(self, obj):
        self.added.append(obj)


def _tools(user_id: uuid.UUID):
    from app.services.builtin_tools import build_presentation_tools

    return build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]


def _run(executor, name: str, arguments: dict):
    from app.tools.base import ToolCall

    return asyncio.run(executor.execute_tool(ToolCall(id="call-1", name=name, arguments=arguments)))


def _slides(count: int) -> list[dict]:
    return [{"layout": "title", "title": f"Slide {i}", "subtitle": "sub"} for i in range(count)]


def _existing_row(user_id, **columns):
    from app.models.presentation import Presentation

    return Presentation(
        id=uuid.uuid4(),
        user_id=user_id,
        title="Deck",
        theme="dark",
        slide_count=2,
        revision=1,
        spec={"title": "Deck", "slides": _slides(2)},
        **columns,
    )


def test_generate_presentation_reports_the_layout_of_the_deck_it_just_saved(monkeypatch):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", lambda: session)
    checked: list[tuple] = []

    async def _fake_check(presentation_id, user_id, spec, *, expected_revision=None):
        checked.append((presentation_id, user_id, spec, expected_revision))
        return LayoutReport(
            slide_count=3,
            findings=(
                LayoutFinding(
                    slide_index=1,
                    slide_id=None,
                    layout="title",
                    title="Slide 1",
                    kind="fit",
                    severity="overloaded",
                    message="the body needed 40% more height",
                    scale=0.71,
                    amount=40,
                ),
            ),
        )

    monkeypatch.setattr(layout, "check_presentation_layout", _fake_check)

    user_id = uuid.uuid4()
    _defs, executor = _tools(user_id)
    result = _run(executor, "generate_presentation", {"title": "Deck", "slides": _slides(3)})

    assert not result.is_error, result.content
    # The check ran on the SAVED deck, as the acting user, on the saved spec,
    # pinned to the revision the save produced.
    ((deck_id, checked_user, spec, expected_revision),) = checked
    row = session.added[0]
    assert deck_id == row.id
    assert checked_user == user_id
    assert spec == row.spec
    assert expected_revision == row.revision
    assert session.committed
    # Findings ride the same result the marker does, before the id line.
    assert 'slide 2 [title] "Slide 1" (overloaded): the body needed 40% more height' in result.content
    assert result.content.index("Layout check:") < result.content.index("presentation_id:")
    assert f"[[PRESENTATION:{row.id}]]" in result.content


def test_generate_presentation_reports_the_save_before_the_layout_render(monkeypatch):
    """The stage refreshes on the COMMIT, not on the tool's return.

    The layout check renders the deck in a headless browser before the tool
    returns; the ``presentation_saved`` progress event must fire after the
    commit and BEFORE that render starts, naming the deck and its revision.
    """
    import app.core.database as database
    from app.tools.tool_events import (
        PRESENTATION_SAVED_EVENT,
        bind_tool_event_sink,
        reset_tool_event_sink,
    )

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", lambda: session)
    checked: list[tuple] = []
    reported: list[dict] = []

    async def _fake_check(presentation_id, user_id, spec, *, expected_revision=None):
        checked.append((presentation_id, expected_revision))
        return LayoutReport(slide_count=2)

    async def _sink(payload: dict) -> None:
        # Snapshot the world at the moment of the report.
        reported.append({**payload, "committed": session.committed, "checks_so_far": len(checked)})

    monkeypatch.setattr(layout, "check_presentation_layout", _fake_check)
    token = bind_tool_event_sink(_sink)
    try:
        _defs, executor = _tools(uuid.uuid4())
        result = _run(executor, "generate_presentation", {"title": "Deck", "slides": _slides(2)})
    finally:
        reset_tool_event_sink(token)

    assert not result.is_error, result.content
    row = session.added[0]
    assert reported == [
        {
            "event": PRESENTATION_SAVED_EVENT,
            "presentation_id": str(row.id),
            "revision": row.revision,
            "committed": True,
            "checks_so_far": 0,
        }
    ]
    # ...and the render still ran, after the report, on that same revision.
    assert checked == [(row.id, row.revision)]


def test_a_failing_check_never_fails_the_save(monkeypatch):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", lambda: session)

    async def _explode(*a, **k):
        raise RuntimeError("browser pool exhausted")

    monkeypatch.setattr(layout, "check_presentation_layout", _explode)

    _defs, executor = _tools(uuid.uuid4())
    result = _run(executor, "generate_presentation", {"title": "Deck", "slides": _slides(2)})

    assert not result.is_error, result.content
    assert session.committed
    assert "Layout check" not in result.content
    assert "browser pool" not in result.content
    assert "[[PRESENTATION:" in result.content


def test_a_clean_check_is_a_single_reassuring_line(monkeypatch):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", lambda: session)

    async def _clean(presentation_id, user_id, spec, **kwargs):
        return LayoutReport(slide_count=2)

    monkeypatch.setattr(layout, "check_presentation_layout", _clean)
    _defs, executor = _tools(uuid.uuid4())
    result = _run(executor, "generate_presentation", {"title": "Deck", "slides": _slides(2)})
    assert "Layout check: every one of the 2 slides fits its canvas." in result.content


def test_get_presentation_measures_for_an_editor_and_not_for_a_reader(monkeypatch):
    import app.core.database as database

    owner, reader = uuid.uuid4(), uuid.uuid4()
    existing = _existing_row(
        owner,
        shared_user_ids=[str(reader)],
        shared_user_permissions={str(reader): "read_only"},
    )
    checks: list[tuple] = []

    async def _fake_check(presentation_id, user_id, spec, *, expected_revision=None):
        checks.append((user_id, expected_revision))
        return LayoutReport(slide_count=2)

    monkeypatch.setattr(layout, "check_presentation_layout", _fake_check)

    # The owner can act on the findings: the read-back carries them - pinned
    # to the revision read, which is how the save's report is reused.
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=existing)])
    monkeypatch.setattr(database, "async_session", lambda: session)
    content = _run(_tools(owner)[1], "get_presentation", {"presentation_id": str(existing.id)}).content
    assert "Layout check: every one of the 2 slides fits its canvas." in content
    assert checks == [(owner, existing.revision)]

    # A read-only grantee could not fix anything: their read stays a read.
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=existing)])
    monkeypatch.setattr(database, "async_session", lambda: session)
    content = _run(_tools(reader)[1], "get_presentation", {"presentation_id": str(existing.id)}).content
    assert "READ-ONLY" in content
    assert "Layout check" not in content
    assert checks == [(owner, existing.revision)]


def test_the_tool_description_announces_the_check_to_the_agent():
    from app.services.presentation_guidance import build_generate_presentation_description

    description = build_generate_presentation_description()
    assert "LAYOUT CHECK" in description
    # The remedy the agent is told is content, not styling.
    assert "cutting content" in description


# ── The REST route ───────────────────────────────────────────────────────────


def _rest_user(user_id=None, *, admin: bool = False):
    return SimpleNamespace(
        id=user_id or uuid.uuid4(),
        email="someone@example.com",
        role="admin" if admin else "user",
        is_admin=admin,
    )


def test_layout_check_route_is_a_read_for_an_editor_and_runs_as_the_caller(monkeypatch, layout_enabled):
    from app.api.v1 import presentations as api

    owner, editor = uuid.uuid4(), uuid.uuid4()
    deck = _existing_row(
        owner,
        shared_user_ids=[str(editor)],
        shared_user_permissions={str(editor): "config"},
    )
    calls: list[tuple] = []

    async def _fake_check(presentation_id, user_id, spec, *, expected_revision=None):
        calls.append((presentation_id, user_id, spec, expected_revision))
        return LayoutReport(slide_count=2, findings=(), revision=expected_revision)

    monkeypatch.setattr(api, "check_presentation_layout", _fake_check)

    # A `config` grantee measures the deck as it is shown to them, and nothing
    # is written. The check is pinned to the revision read, and the payload
    # says which one it describes.
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=deck)])
    payload = asyncio.run(api.layout_check_presentation_endpoint(str(deck.id), user=_rest_user(editor), db=session))
    assert payload == {"slide_count": 2, "skipped": None, "unit": "slide", "revision": deck.revision, "findings": []}
    assert calls == [(deck.id, editor, deck.spec, deck.revision)]
    assert session.added == [] and not session.committed


def test_layout_check_route_refuses_a_read_only_reader(monkeypatch, layout_enabled):
    """A reader could act on no finding, like ``get_presentation``,
    which never measures for them: a deck they can see answers the manage-tier
    403 of the other editing routes, and no render is spent on it."""
    from fastapi import HTTPException

    from app.api.v1 import presentations as api

    owner, reader = uuid.uuid4(), uuid.uuid4()
    deck = _existing_row(
        owner,
        shared_user_ids=[str(reader)],
        shared_user_permissions={str(reader): "read_only"},
    )

    async def _never(*a, **k):
        raise AssertionError("the check must not run for a reader")

    monkeypatch.setattr(api, "check_presentation_layout", _never)
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=deck)])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.layout_check_presentation_endpoint(str(deck.id), user=_rest_user(reader), db=session))
    assert exc.value.status_code == 403
    assert exc.value.detail == "You can view this presentation but not edit it"


def test_layout_check_route_returns_a_skipped_report_as_a_200(monkeypatch, layout_enabled):
    from app.api.v1 import presentations as api

    owner = uuid.uuid4()
    deck = _existing_row(owner)

    async def _skipped(presentation_id, user_id, spec, **kwargs):
        return LayoutReport(slide_count=0, skipped="the headless browser is not available on this deployment")

    monkeypatch.setattr(api, "check_presentation_layout", _skipped)
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=deck)])
    payload = asyncio.run(api.layout_check_presentation_endpoint(str(deck.id), user=_rest_user(owner), db=session))
    assert payload["skipped"] == "the headless browser is not available on this deployment"
    assert payload["findings"] == []


def test_layout_check_route_is_404_when_disabled_and_for_an_invisible_deck(monkeypatch, layout_enabled):
    from fastapi import HTTPException

    from app.api.v1 import presentations as api
    from app.core.config import settings

    async def _never(*a, **k):
        raise AssertionError("the check must not run")

    monkeypatch.setattr(api, "check_presentation_layout", _never)

    # Filtered out in SQL -> 404 like every other deck read.
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=None)])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.layout_check_presentation_endpoint(str(uuid.uuid4()), user=_rest_user(), db=session))
    assert exc.value.status_code == 404

    # Disabled on the deployment -> 404 before any lookup.
    monkeypatch.setattr(settings, "presentation_layout_check_enabled", False)
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=_existing_row(uuid.uuid4()))])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.layout_check_presentation_endpoint(str(uuid.uuid4()), user=_rest_user(), db=session))
    assert exc.value.status_code == 404
    assert "disabled" in exc.value.detail
    assert session._results, "no lookup ran"


def test_layout_check_route_is_registered_as_a_post_under_the_deck():
    from app.api.v1.presentations import router

    routes = [(r.path, sorted(r.methods)) for r in router.routes if getattr(r, "path", "").endswith("/layout-check")]
    assert routes == [("/{presentation_id}/layout-check", ["POST"])]


# ── The two halves agree: browser side and Python side ───────────────────────


def test_the_audit_script_reads_what_the_fit_hook_publishes():
    """``fit.ts`` writes ``data-fit`` on the ``.deck-fit`` host; the audit reads
    exactly that, and the PPTX extractor corrects its measurements by it. One
    attribute name in three places - a rename in one is a silent zero in the
    others."""
    from app.services.presentation_export import EXTRACT_PRIMITIVES_JS, LAYOUT_AUDIT_JS

    fit = (PRESENTATION_DIR / "fit.ts").read_text(encoding="utf-8")
    assert "host.dataset.fit = k.toFixed(3)" in fit
    assert 'host.style.setProperty("--deck-fit", String(k))' in fit
    assert 'querySelectorAll(".deck-fit[data-fit]")' in LAYOUT_AUDIT_JS
    assert 'closest(".deck-fit[data-fit]")' in EXTRACT_PRIMITIVES_JS
    assert "parseFloat(host.dataset.fit)" in LAYOUT_AUDIT_JS
    assert "parseFloat(host.dataset.fit)" in EXTRACT_PRIMITIVES_JS


def test_a_block_that_sets_its_size_is_held_by_the_fit_and_reported_when_let_go():
    """The grid marks itself held (``data-fit-hold``, its block type);
    the stylesheet hands every held block the inverse of the ratio
    (``--deck-fit-hold``) and every grid length is multiplied by it, so the
    scaled body draws it at its size; ``fit.ts`` fits the body around it and
    lets go (``data-fit-released``, the hold back to 1) only when the body
    still overflows at the floor - which the audit reports per held block,
    naming the type and size the grid publishes. The PPTX extractor needs
    nothing new: a held length computes at ``size / k`` and ``fitOf``
    multiplies it back by ``k``."""
    from app.services.presentation_export import EXTRACT_PRIMITIVES_JS, LAYOUT_AUDIT_JS

    metrics = (PRESENTATION_DIR / "blocks" / "metrics.tsx").read_text(encoding="utf-8")
    assert 'data-fit-hold="metrics_grid"' in metrics
    assert 'data-size={block.size ?? "md"}' in metrics

    css = (PRESENTATION_DIR / "presentation.css").read_text(encoding="utf-8")
    assert re.search(r"\.deck-fit-content\s*\{[^}]*--deck-fit-hold:\s*calc\(1 / var\(--deck-fit, 1\)\);", css)
    assert re.search(r"\.deck-fit\[data-fit-released\] > \.deck-fit-content\s*\{\s*--deck-fit-hold:\s*1;", css)
    assert "--deck-metric-hold: var(--deck-fit-hold, 1);" in css
    assert "--deck-metric-k: calc(var(--deck-metric-scale) * var(--deck-metric-hold));" in css
    # Every length a grid tile is drawn with goes through the hold.
    for needle in (
        "gap: calc(var(--deck-metric-gap) * var(--deck-metric-hold));",
        "padding: calc(var(--deck-metric-pad-y) * var(--deck-metric-k)) calc(var(--deck-metric-pad-x) * var(--deck-metric-k));",
        "border-radius: calc(var(--deck-radius) * var(--deck-metric-hold));",
        "font-size: calc(var(--deck-metric-value-fs) * var(--deck-metric-k));",
        "font-size: calc(var(--deck-metric-label-fs) * var(--deck-metric-hold));",
        "font-size: calc(var(--deck-metric-note-fs) * var(--deck-metric-hold));",
        "height: calc(40px * var(--deck-metric-k));",
    ):
        assert needle in css, needle
    # No grid rule draws a fixed length the hold would miss (a zero needs no hold).
    grid_rules = re.findall(r"\.deck-root \.deck-metrics[^{]*\{([^}]*)\}", css)
    for body in grid_rules:
        for declaration in body.split(";"):
            sized = re.search(r"^\s*(font-size|padding|gap|width|min-width|height|margin-top|margin-bottom)\s*:", declaration)
            if sized and not re.search(r":\s*0\s*$", declaration):
                assert "var(--deck-metric-" in declaration, declaration

    fit = (PRESENTATION_DIR / "fit.ts").read_text(encoding="utf-8")
    assert 'export const FIT_HOLD_ATTR = "data-fit-hold";' in fit
    assert 'if (released) host.dataset.fitReleased = "true";' in fit
    assert "if (host.dataset.fitReleased !== undefined) releaseHold(host, false);" in fit
    assert "!content.querySelector(`[${FIT_HOLD_ATTR}]`)" in fit
    assert "releaseHold(host, true);\n  return fitPass(host, content, available);" in fit

    assert 'if (!host.hasAttribute("data-fit-released")) continue;' in LAYOUT_AUDIT_JS
    assert 'host.querySelectorAll("[data-fit-hold]")' in LAYOUT_AUDIT_JS
    assert 'kind: "size", block: held.dataset.fitHold, size: held.dataset.size || "", scale' in LAYOUT_AUDIT_JS
    assert "size: px(cs.fontSize) * k" in EXTRACT_PRIMITIVES_JS


def test_every_slide_body_is_wrapped_in_the_fit_host():
    """The shrink-to-fit is applied in ``SlideBodyArea`` (the shared frame), so
    every layout gets it without opting in - and the static render the audit
    measures is the same component tree, so what it measures is what is shown."""
    frame = (PRESENTATION_DIR / "layouts" / "frame.tsx").read_text(encoding="utf-8")
    assert "useFitToHeight(hostRef, contentRef)" in frame
    assert 'className="deck-fit"' in frame
    assert 'className="deck-fit-content"' in frame
    css = (PRESENTATION_DIR / "presentation.css").read_text(encoding="utf-8")
    assert ".deck-fit-content" in css
    # The scaled content must anchor at the top-left, or the shrink drifts.
    assert re.search(r"\.deck-fit-content\s*\{[^}]*transform-origin:\s*top left", css)


def test_the_audit_kinds_and_severities_are_the_frontend_types():
    """The panel's icon map is keyed by kind and its badge map by severity; a
    kind the backend adds without a frontend entry renders with the fallback
    icon, a severity without one crashes the panel."""
    kinds = set(layout.FindingKind.__args__)
    severities = set(layout.Severity.__args__)

    types_ts = (FRONTEND_ROOT / "src" / "types" / "index.ts").read_text(encoding="utf-8")
    kind_union = re.search(r"export type PresentationLayoutFindingKind = ([^;]+);", types_ts)
    severity_union = re.search(r"export type PresentationLayoutSeverity = ([^;]+);", types_ts)
    assert kind_union and severity_union
    assert set(re.findall(r'"(\w+)"', kind_union.group(1))) == kinds
    assert set(re.findall(r'"(\w+)"', severity_union.group(1))) == severities

    panel = (PRESENTATION_DIR / "PresentationLayoutPanel.tsx").read_text(encoding="utf-8")
    # Skip the type annotation: the object literal starts at the first ``= {``.
    kind_labels = re.search(r"const KIND_LABELS = \{(.*?)\n\}", panel, re.S)
    kind_icons = re.search(r"const KIND_ICONS.*?= \{(.*?)\n\};", panel, re.S)
    severity_config = re.search(r"const SEVERITY_CONFIG.*?= \{(.*?)\n\};", panel, re.S)
    assert kind_labels and kind_icons and severity_config
    assert set(re.findall(r"^\s*(\w+):", kind_labels.group(1), re.M)) == kinds
    assert set(re.findall(r"^\s*(\w+):", kind_icons.group(1), re.M)) == kinds
    assert set(re.findall(r"^\s*(\w+):\s*\{", severity_config.group(1), re.M)) == severities
    # The primed message keeps the agent's bound so both read the same list.
    assert f"const MAX_PRIMED_FINDINGS = {MAX_REPORTED_FINDINGS};" in panel


def test_decorative_text_is_left_out_of_the_measurements():
    """The section layout paints a ghost number past the slide's edge by
    design (``aria-hidden``). Skipping the element is not enough: its glyphs
    are also accumulated as the FRAME's extents, and the frame clips, so the
    walker must skip them too - or every section slide reports "the slide is
    cut off at its bottom edge". The chrome footer is aria-hidden as well and
    keeps its own scroll-width check, which is what still catches a long one."""
    from app.services.presentation_export import LAYOUT_AUDIT_JS

    opening = (PRESENTATION_DIR / "layouts" / "opening.tsx").read_text(encoding="utf-8")
    assert 'className="deck-section-number" aria-hidden="true"' in opening
    chrome = (PRESENTATION_DIR / "chrome.tsx").read_text(encoding="utf-8")
    assert 'className="deck-chrome" aria-hidden="true"' in chrome

    # Element pass, glyph walker, the cut-text excerpt and the logo contrast
    # pass (the chrome's own mark is inked by the chrome): four skips.
    assert LAYOUT_AUDIT_JS.count("""closest("[aria-hidden='true']")""") == 4
    walker = LAYOUT_AUDIT_JS.index("createTreeWalker(slide, NodeFilter.SHOW_TEXT)")
    skip = LAYOUT_AUDIT_JS.index("""node.parentElement.closest("[aria-hidden='true']")""")
    assert walker < skip < LAYOUT_AUDIT_JS.index("range.selectNodeContents(node)")
    # The footer and the template header are checked as bars of their
    # own: each is one ellipsized line, so a cut is a real loss.
    assert '[".deck-chrome-footer", "chrome-footer"]' in LAYOUT_AUDIT_JS
    assert '[".deck-chrome-header", "chrome-header"]' in LAYOUT_AUDIT_JS
    assert "bar.scrollWidth - bar.clientWidth" in LAYOUT_AUDIT_JS


def test_the_dashboard_and_its_charts_are_measured_and_named_by_their_block():
    """The ``dashboard`` layout is covered by the shrink-to-fit check
    like every other layout - its body is a ``SlideBodyArea`` - and a finding
    inside it names what the agent can find in its spec: the ``panel`` (or
    the chart / table it holds), never the grid, the tile row or the chart's
    own scaffolding (legend, canvas, swatches, the hidden facts element). A
    scaffolding class the audit does not know would become the finding's
    name, so every ``deck-*`` class the two components use as structure must
    be in the audit's list, and the block names must not."""
    from app.services.presentation_export import LAYOUT_AUDIT_JS

    dashboard = (PRESENTATION_DIR / "layouts" / "dashboard.tsx").read_text(encoding="utf-8")
    assert "<SlideBodyArea>" in dashboard and "</SlideBodyArea>" in dashboard
    assert '<div className="deck-dashboard"' in dashboard
    assert 'className="deck-dashboard-tiles"' in dashboard
    assert 'className="deck-dashboard-panels"' in dashboard
    assert 'className="deck-panel"' in dashboard
    assert 'className="deck-panel-body"' in dashboard

    structural = re.search(r"const STRUCTURAL = /\^deck-\((.*)\)\$/;", LAYOUT_AUDIT_JS)
    assert structural, "the audit lost its STRUCTURAL class list"
    listed = structural.group(1)
    # A dashboard's scaffolding is structure; the panel is the block.
    for name in ("dashboard", "dashboard-tiles", "dashboard-panels", "panel-body"):
        assert re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", listed), f"{name} missing from STRUCTURAL"
    assert not re.search(r"(?<![\w-])panel(?![\w-])", listed), "the panel IS the block a finding names"

    chart = (PRESENTATION_DIR / "blocks" / "chart.tsx").read_text(encoding="utf-8")
    canvas = (PRESENTATION_DIR / "blocks" / "ChartCanvas.tsx").read_text(encoding="utf-8")
    map_canvas = (PRESENTATION_DIR / "blocks" / "MapCanvas.tsx").read_text(encoding="utf-8")
    assert 'className="deck-chart"' in chart, "the figure carries the block name"
    used = set(re.findall(r"deck-chart-([a-z][a-z-]*[a-z])", chart + canvas + map_canvas))
    chart_group = re.search(r"chart-\(([^)]*)\)", listed)
    assert chart_group, "the audit lost the chart scaffolding group"
    known = set(chart_group.group(1).split("|"))
    # Every structural chart class the components render is known to the
    # audit (a value label, the legend, the hidden facts, the gauge reading).
    missing = {f"chart-{name}" for name in used} - {f"chart-{name}" for name in known}
    assert not missing, f"chart scaffolding the audit would name as a block: {sorted(missing)}"
    assert not re.search(r"(?<![\w-])chart(?![\w-])", listed), "the chart IS the block a finding names"


def test_the_static_slide_publishes_its_id_for_the_audit():
    """A finding is anchored to the slide's stable id when it has one, so the
    UI can locate it after a reorder; the static slide must expose it."""
    static = (PRESENTATION_DIR / "StaticSlide.tsx").read_text(encoding="utf-8")
    assert "data-slide-id={slide.id}" in static
    from app.services.presentation_export import LAYOUT_AUDIT_JS

    assert "slide.dataset.slideId" in LAYOUT_AUDIT_JS


def test_the_static_page_publishes_the_revision_the_check_is_pinned_to():
    """``render-spec`` returns the deck's current row; the page writes its
    revision next to the slides it rendered, and the check compares it with the
    revision it was asked about. The panel reads the same field off the
    response to mark the report stale."""
    from app.services.presentation_export import RENDERED_REVISION_JS

    page = (FRONTEND_ROOT / "src" / "pages" / "PresentationStaticRender.tsx").read_text(encoding="utf-8")
    assert "data-deck-revision={presentation.revision}" in page
    assert 'querySelector(".deck-root")' in RENDERED_REVISION_JS
    assert "dataset.deckRevision" in RENDERED_REVISION_JS

    types_ts = (FRONTEND_ROOT / "src" / "types" / "index.ts").read_text(encoding="utf-8")
    check_type = re.search(r"export interface PresentationLayoutCheck \{(.*?)\n\}", types_ts, re.S)
    assert check_type
    assert re.search(r"^\s*revision: number \| null;", check_type.group(1), re.M)
    assert re.search(r'^\s*unit: "slide" \| "page";', check_type.group(1), re.M)
    run = (PRESENTATION_DIR / "useLayoutCheckRun.ts").read_text(encoding="utf-8")
    assert "report.revision ?? requested" in run


def test_the_check_outlives_the_panel_and_the_toolbar_shows_it_running():
    """A check renders the deck for seconds, and the viewer unmounts the panel
    when it is hidden. The run must therefore be held by the VIEWER
    (`useLayoutCheckRun`) and handed to the panel, so closing the panel hides
    the report and not the run; the toolbar button spins for as long as a check
    is in flight, panel open or hidden, says so to assistive technology, and
    the panel only asks for a check when it has nothing current to show."""
    viewer = (FRONTEND_ROOT / "src" / "pages" / "PresentationViewer.tsx").read_text(encoding="utf-8")
    assert "useLayoutCheckRun(data)" in viewer
    assert "check={layoutCheck}" in viewer
    assert "aria-busy={layoutCheck.isPending || undefined}" in viewer
    assert 'data-testid="presentation.layoutCheck.running"' in viewer
    assert "{layoutCheck.isPending && (" in viewer

    panel = (PRESENTATION_DIR / "PresentationLayoutPanel.tsx").read_text(encoding="utf-8")
    assert "useCheckPresentationLayout" not in panel, "the panel must not own the request"
    assert "if (isPending || (report !== null && !report.skipped && !stale)) return;" in panel

    run = (PRESENTATION_DIR / "useLayoutCheckRun.ts").read_text(encoding="utf-8")
    # Scoped to the deck: the viewer is reused across `:id`, and a pending
    # flag or a report read against another deck would spin the wrong button.
    assert "isPending: forThisDeck && isPending" in run
    assert "requestedFor === id" in run
    # The report and the revision it describes are ONE state update, recorded
    # from the success callback — not the report read off the mutation's
    # `data`, which lands separately and could be paired with another run's
    # revision.
    assert "setChecked({ id, revision: report.revision ?? requested, report })" in run
    assert "report: measured?.report ?? null" in run
    # Idempotent while in flight: `isPending` only flips on the render after
    # `mutate`, so two callers in one tick would render the deck twice and
    # `useMutation` would keep the callbacks of the latest call only.
    assert "if (inFlight.current === id) return;" in run
    assert "inFlight.current = id;" in run


def test_the_fit_floor_is_the_renderers_and_a_fitted_host_clips():
    """Below ``FIT_MIN`` the text is unreadable, so ``fit.ts`` stops scaling;
    what still does not fit must then be CLIPPED by the host, or the body paints
    under the footer block and the chrome - the overlap the whole feature
    exists to prevent. The audit reports that cut on the ``fit`` observation and
    ``FIT_FLOOR`` is how the message knows the ratio it reads IS the floor."""
    from app.services.presentation_export import LAYOUT_AUDIT_JS

    fit = (PRESENTATION_DIR / "fit.ts").read_text(encoding="utf-8")
    assert f"export const FIT_MIN = {FIT_FLOOR};" in fit
    css = (PRESENTATION_DIR / "presentation.css").read_text(encoding="utf-8")
    # Only a host that HAD to shrink clips: a body within budget keeps its
    # shadows and focus rings.
    assert re.search(r"\.deck-fit\[data-fit\]\s*\{[^}]*overflow:\s*hidden", css)
    assert 'findings.push({ kind: "fit", scale, cut:' in LAYOUT_AUDIT_JS
    # ...and the element pass leaves the host alone, or the same cut is
    # reported twice (once as ``fit``, once as ``clipped``).
    assert 'el.classList.contains("deck-fit")' in LAYOUT_AUDIT_JS


def test_the_fit_ratio_is_bracketed_not_iterated_to_a_fixed_point():
    """The fixed-point step ``k' = available / height(k)`` oscillates on text
    that reflows (a true fit of 0.8 runs ``1 -> 0.64 -> 1 -> 0.64``), and a
    belt applied at the end over-shrinks readable content into a false
    ``overloaded`` finding. ``fit.ts`` therefore brackets the fit in a pure
    ``searchFit`` (every layout read through a callback, so the search can be
    exercised without a DOM), always answers with the end that FITS, and stops
    on the slack allowance that also makes a second pass a no-op. The ratio is
    never rounded up: a ratio rounded up by a hair is one that does not fit."""
    fit = (PRESENTATION_DIR / "fit.ts").read_text(encoding="utf-8")
    assert "export function searchFit(" in fit
    assert "searchFit(available, startK, startHeight, (candidate) =>" in fit
    assert "return lo ? lo.k : FIT_MIN;" in fit
    assert "export const FIT_SLACK_RATIO = 0.01;" in fit
    assert "export const FIT_MAX_PROBES = 8;" in fit
    assert "Math.floor(k * 1000 + 1e-9) / 1000" in fit
    assert "FIT_MAX_ROUNDS" not in fit


def test_the_viewer_hides_the_layout_check_where_the_deployment_disabled_it():
    """``PRESENTATION_LAYOUT_CHECK_ENABLED=false`` makes the REST route 404, so
    the switch travels to the frontend on the public auth config (the
    ``runners_enabled`` pattern) and the viewer offers neither the button nor
    the panel, instead of a control whose only content is that 404. A reader
    is offered neither either: the route refuses them. The panel is
    also keyed by the deck: the router reuses the page when only ``:id``
    changes, and a report belongs to the deck it was made for."""
    from app.api.v1.auth import AuthConfigResponse

    field = AuthConfigResponse.model_fields["presentation_layout_check_enabled"]
    assert field.default is True
    auth = (BACKEND_ROOT / "app" / "api" / "v1" / "auth.py").read_text(encoding="utf-8")
    assert "presentation_layout_check_enabled=settings.presentation_layout_check_enabled" in auth
    types_ts = (FRONTEND_ROOT / "src" / "types" / "index.ts").read_text(encoding="utf-8")
    assert "presentation_layout_check_enabled?: boolean;" in types_ts
    viewer = (FRONTEND_ROOT / "src" / "pages" / "PresentationViewer.tsx").read_text(encoding="utf-8")
    assert "useAuthConfig().data?.presentation_layout_check_enabled ?? true" in viewer
    assert "const layoutCheckOffered = layoutCheckEnabled && data.can_manage;" in viewer
    assert '(panel === "layout" && layoutCheckOffered)' in viewer
    assert 'panel === "layout" && layoutCheckOffered && (' in viewer
    assert "layoutCheckOffered ? (" in viewer
    assert "layoutCheckEnabled ? (" not in viewer
    assert re.search(r"<PresentationLayoutPanel\s+key=\{data\.id\}", viewer)


def test_the_panel_locates_a_slide_by_its_stable_id():
    """A finding's ``slide_index`` is the slide's position WHEN MEASURED. The
    panel resolves the stable id against the current spec, so after a reorder
    the row still opens the right slide, and a removed slide is not confused
    with whatever now sits at its old index."""
    panel = (PRESENTATION_DIR / "PresentationLayoutPanel.tsx").read_text(encoding="utf-8")
    assert "function currentSlideIndex(" in panel
    assert "slides.findIndex((slide) => slide.id === group.slideId)" in panel
    assert "currentSlideIndex(group, slides)" in panel
    assert "disabled={gone}" in panel


def test_the_panel_strings_are_in_every_catalog():
    """The panel is user-facing in four languages; a key missing from one
    catalog renders the English source in that language."""
    panel = (PRESENTATION_DIR / "PresentationLayoutPanel.tsx").read_text(encoding="utf-8")
    keys = set(re.findall(r't_i18n\(\s*"([^"]+)"', panel))
    # Labels looked up indirectly through the config maps.
    keys |= {"Shrunk to fit", "Cut off", "Runs past its line", "Off the canvas", "Leaves the bottom empty"}
    keys |= {"Dense", "Overloaded", "Advisory"}
    # The toolbar button's labels, which the viewer owns.
    keys |= {"Hide the layout check", "Layout check: rendering {count} {unit}..."}
    assert "Layout check" in keys and "Fix with my assistant" in keys
    for lang in ("en", "fr", "es", "ja"):
        catalog = json.loads((FRONTEND_ROOT / "src" / "i18n" / "lang" / f"{lang}.json").read_text(encoding="utf-8"))
        missing = sorted(k for k in keys if k not in catalog)
        assert not missing, f"{lang}.json is missing {missing}"


def test_the_clean_state_does_not_claim_nothing_was_shrunk():
    """A body fitted between ``FIT_DENSE_BELOW`` and 1 is deliberately not
    reported, so a report with no findings can still describe slightly shrunk
    slides. The panel's clean state therefore says what IS true - nothing cut
    off, nothing crowded enough to need a change - and the backend's one-liner
    only says the slides fit."""
    slides = [_slide(0, [{"kind": "fit", "scale": 0.95}]), _slide(1, [{"kind": "fit", "scale": 0.93}])]
    assert findings_from_observations(slides, _spec(["A", "B"])) == []
    panel = (PRESENTATION_DIR / "PresentationLayoutPanel.tsx").read_text(encoding="utf-8")
    assert "Nothing is cut off and nothing is crowded enough to need a change." in panel
    assert "nothing had to be shrunk to fit" not in panel
    report = LayoutReport(slide_count=2, findings=[])
    assert format_layout_report(report) == "Layout check: every one of the 2 slides fits its canvas."


def test_the_switch_is_documented():
    """An env var the deployment can flip must be in ``.env.sample`` and in the
    configuration docs (the env-docs CI check enforces the second half; this
    pins the first so a rename cannot orphan the documented name)."""
    from app.core.config import Settings

    assert "presentation_layout_check_enabled" in Settings.model_fields
    sample = (BACKEND_ROOT.parent / ".env.sample").read_text(encoding="utf-8")
    assert "PRESENTATION_LAYOUT_CHECK_ENABLED=" in sample
    docs = (BACKEND_ROOT.parent / "docs" / "docs" / "deployment" / "configuration.md").read_text(encoding="utf-8")
    assert "PRESENTATION_LAYOUT_CHECK_ENABLED" in docs
