"""Even logo cells, four columns side by side, and logos on timeline steps.

A ``logo_grid`` with ``columns`` shares its width in equal
cells again (``align: even``, the default whenever ``columns`` is set), a
``columns`` block of four keeps them side by side unless their content cannot
fit - the wrap is reported by the layout audit - and a ``timeline`` step
carries its own logo. What is pinned here is everything but the pixels, which
are ``tests_e2e/presentation-logo-layout.spec.ts``'s:

* the spec: ``align`` unset by default, ``even`` accepted, a step's logo fields;
* where a step's logo is read (its geometry, the picture list, a layout switch);
* the audit's ``columns`` finding;
* the renderer contracts the e2e measures (the tracks, the wrap attribute).
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from app.services.presentation_export import LAYOUT_AUDIT_JS
from app.services.presentation_layout import findings_from_observations
from app.services.presentation_layout_switch import switch_slide_layout
from app.services.presentation_pictures import picture_placements
from app.services.presentation_spec import (
    PresentationSpecError,
    logo_file_ids,
    spec_reference,
    validate_spec,
)

FRONTEND_PRESENTATION = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation"


def _read(*parts: str) -> str:
    return FRONTEND_PRESENTATION.joinpath(*parts).read_text(encoding="utf-8")


def _ids(count: int) -> list[str]:
    return [str(uuid.UUID(int=index + 1)) for index in range(count)]


def _deck(*blocks: dict) -> dict:
    return {"title": "Logos", "slides": [{"layout": "content", "title": "Funding", "blocks": list(blocks)}]}


def _stored_block(spec: dict) -> dict:
    return validate_spec(spec)["slides"][0]["blocks"][0]


def _grid(count: int = 4, **fields) -> dict:
    return {"type": "logo_grid", "items": [{"file_id": file_id} for file_id in _ids(count)], **fields}


def _rounds(logos: list[str | None]) -> dict:
    items = []
    for index, logo in enumerate(logos):
        item = {"label": f"202{index}", "title": f"Round {index + 1}", "description": "Led by a fund"}
        if logo:
            item["logo_file_id"] = logo
        items.append(item)
    return {"type": "timeline", "style": "phases", "items": items}


# ── The spec ─────────────────────────────────────────────────────────────────


def test_a_grid_stores_no_alignment_unless_one_is_chosen():
    """Unset is what makes `even` the default WITH columns and `stretch` without:
    a stored default would pin one of them whatever `columns` says later."""
    assert "align" not in _stored_block(_deck(_grid(columns=4)))
    assert "align" not in _stored_block(_deck(_grid()))
    for align in ("start", "center", "even", "stretch"):
        assert _stored_block(_deck(_grid(columns=4, align=align)))["align"] == align


def test_an_unknown_alignment_is_refused():
    with pytest.raises(PresentationSpecError) as caught:
        validate_spec(_deck(_grid(align="between")))
    assert "align" in str(caught.value)


def test_a_timeline_step_takes_a_logo_and_stores_its_look():
    logo = _ids(1)[0]
    block = _stored_block(_deck(_rounds([logo, None])))
    with_logo, without = block["items"]
    assert with_logo["logo_file_id"] == logo
    # The look is materialized like every other default; it draws only with a logo.
    assert (with_logo["logo_height"], with_logo["framed"]) == ("sm", True)
    assert "logo_file_id" not in without
    sized = _rounds([logo, logo])
    sized["items"][1].update(logo_height="lg", framed=False)
    stored = _stored_block(_deck(sized))["items"][1]
    assert (stored["logo_height"], stored["framed"]) == ("lg", False)


@pytest.mark.parametrize("item", [{"logo_height": "xl"}, {"logo_file_id": "x" * 37}, {"framed": "maybe"}])
def test_a_step_logo_out_of_bounds_is_refused(item):
    spec = _rounds([_ids(1)[0], None])
    spec["items"][0].update(item)
    with pytest.raises(PresentationSpecError):
        validate_spec(_deck(spec))


def test_the_reference_teaches_even_cells_and_step_logos():
    reference = spec_reference()
    assert '"start"|"center"|"even"|"stretch"' in reference
    assert "even = equal cells (default with columns)" in reference
    timeline_item = next(line for line in reference.splitlines() if line.startswith("- TimelineItem:"))
    for needle in ("logo_file_id?", 'logo_height?: Size = "sm"', "framed?: bool = true"):
        assert needle in timeline_item, needle


# ── Rows of equal cards ──────────────────────────────────────────────


def _cards_slide(**fields) -> dict:
    card = {"type": "phase_card", "title": "Phase", "items": ["One", "Two"]}
    return {"layout": "content", "title": "Offer", "columns": [[card, card], [card, card]], **fields}


def test_columns_line_their_blocks_up_as_rows_only_when_asked():
    stored = validate_spec({"title": "Deck", "slides": [_cards_slide()]})["slides"][0]
    assert stored["equal_rows"] is False
    assert validate_spec({"title": "Deck", "slides": [_cards_slide(equal_rows=True)]})["slides"][0]["equal_rows"] is True
    columns = {"type": "columns", "columns": [[{"type": "text", "content": "A"}], [{"type": "text", "content": "B"}]]}
    assert _stored_block(_deck(columns))["equal_rows"] is False
    assert _stored_block(_deck(columns | {"equal_rows": True}))["equal_rows"] is True
    reference = spec_reference()
    assert reference.count("equal_rows?: bool = false (the Nth block of every column shares one height") == 2


def test_a_sparse_slide_of_cards_is_never_told_to_stretch_them():
    """Blocks are sized to their content: spread columns or lined-up
    rows, the remedy never names `distribute` or `fill`."""
    from app.services.presentation_layout import sparse_remedy

    for slide in (_cards_slide(), _cards_slide(equal_rows=True)):
        remedy = sparse_remedy({"slides": [slide]}, 0, "slide")
        assert remedy == "Add content to the body or add a footer_block takeaway."
        assert "distribute" not in remedy.lower()


def test_columns_lined_up_as_rows_are_subgrid_rows_and_are_not_spread():
    css = _read("presentation.css")
    assert re.search(
        r'\.deck-columns\[data-equal-rows="true"\]:not\(\[data-wrapped\]\) \{\s*grid-template-rows: '
        r"repeat\(var\(--deck-column-rows, 1\), auto\);",
        css,
    )
    assert re.search(
        r'\[data-equal-rows="true"\]:not\(\[data-wrapped\]\) > \.deck-column > \.deck-blocks \{\s*display: grid;\s*'
        r"grid-row: 1 / -1;\s*grid-template-rows: subgrid;",
        css,
    )
    body = _read("layouts", "body.tsx")
    assert "const spread = slide.distribute && !slide.equal_rows;" in body
    assert "{...equalRowsAttributes(slide.equal_rows, slide.columns)}" in body
    assert "{...equalRowsAttributes(block.equal_rows, block.columns)}" in _read("blocks", "containers.tsx")
    # Off: no attribute, no style - the DOM of every stored deck.
    assert "if (!enabled) return {};" in _read("columns-fit.ts")


# ── Where a step's logo is read ──────────────────────────────────────────────


def test_a_step_logo_is_a_deck_logo_whose_geometry_is_read_and_served():
    """`logo_file_ids` is what the save backfills the trim box of and what the
    deck read serves as `asset_meta`: a step's logo is cropped to its mark
    like a grid's, in deck order with the others and once."""
    first, second, shared = _ids(3)
    spec = validate_spec(_deck(_grid(1) | {"items": [{"file_id": shared}]}, _rounds([first, None, second, shared])))
    assert logo_file_ids(spec) == [shared, first, second]


def test_a_step_logo_is_a_picture_named_by_its_step():
    logo = _ids(1)[0]
    spec = validate_spec(_deck(_rounds([None, logo])))
    assert [placement.label() for placement in picture_placements(spec)] == [
        'slide 1 block 1 (timeline) item 2 logo_file_id "2021"'
    ]


def test_a_layout_switch_that_lists_words_says_it_dropped_the_step_logos():
    logos = _ids(2)
    slide = {"layout": "content", "title": "Funding", "blocks": [_rounds([logos[0], None, logos[1]])]}
    outcome = switch_slide_layout(slide, "takeaways")
    assert "The timeline's 2 step logos - a takeaways slide lists words" in outcome.dropped
    # The steps' words are listed, only their logos are left behind.
    assert [item["title"] for item in outcome.slide["items"]] == ["Round 1", "Round 2", "Round 3"]


# ── The audit ────────────────────────────────────────────────────────────────


def _spec(titles: list[str]) -> dict:
    return {"title": "Deck", "slides": [{"layout": "content", "title": t} for t in titles]}


def _slide(observations: list[dict]) -> dict:
    return {"index": 0, "slide_id": "s0", "layout": "content", "findings": observations}


def _wrapped(**fields) -> dict:
    return {"kind": "columns", "count": 4, "per_row": 2, "need": 520, "room": 396, "block": "columns", **fields}


def test_four_columns_wrapped_are_a_dense_finding_with_both_widths():
    (finding,) = findings_from_observations([_slide([_wrapped()])], _spec(["Partners"]))
    assert (finding.kind, finding.severity, finding.amount) == ("columns", "dense", 520)
    assert "cannot keep its 4 columns side by side" in finding.message
    assert "needs 520px and each column gets 396px" in finding.message
    assert "wrap onto rows of 2" in finding.message
    assert "or use fewer columns" in finding.message


@pytest.mark.parametrize("fields", [{"per_row": 4}, {"per_row": 0}, {"count": 1}, {"per_row": "x"}])
def test_an_observation_that_wrapped_nothing_is_no_finding(fields):
    assert findings_from_observations([_slide([_wrapped(**fields)])], _spec(["A"])) == []


def test_a_wrapped_block_is_never_also_told_the_slide_is_sparse():
    sparse = {"kind": "sparse", "unused": 0.6, "empty": 500, "slide": 1080}
    assert [finding.kind for finding in findings_from_observations([_slide([_wrapped(), sparse])], _spec(["A"]))] == [
        "columns"
    ]


def test_the_audit_reads_the_wrap_the_renderer_decided():
    assert '".deck-columns[data-wrapped]"' in LAYOUT_AUDIT_JS
    for attribute in ("block.dataset.count", "block.dataset.wrapped", "block.dataset.columnNeed", "block.dataset.columnRoom"):
        assert attribute in LAYOUT_AUDIT_JS, attribute
    # The renderer writes what the audit reads.
    fit = _read("columns-fit.ts")
    for key in ("wrapped:", "columnNeed:", "columnRoom:"):
        assert key in fit, key


# ── The renderer contracts the e2e spec measures ─────────────────────────────


def test_four_columns_are_four_tracks_unless_wrapped_and_nothing_else_moved():
    css = _read("presentation.css")
    assert re.search(
        r'\.deck-columns\[data-count="4"\]:not\(\[data-wrapped\]\)\s*\{\s*grid-template-columns:\s*repeat\(4, 1fr\);',
        css,
    )
    # Two and three columns keep their historical tracks.
    assert re.search(r"\.deck-root \.deck-columns \{\s*display: grid;\s*grid-template-columns: 1fr 1fr;", css)
    assert '.deck-root .deck-columns[data-count="3"] { grid-template-columns: repeat(3, 1fr); }' in css
    containers = _read("blocks", "containers.tsx")
    assert "useColumnsFit(ref, block.columns);" in containers
    math = _read("columns-fit-math.ts")
    assert "export const WRAPPING_COLUMN_COUNT = 4;" in math
    assert "export const WRAPPED_PER_ROW = 2;" in math


def test_an_even_grid_sizes_its_cells_for_a_full_row():
    """The equal cells are keyed on `data-equal-cells`, which `even` sets and
    `start` under `columns` sets too: one rule for both."""
    css = _read("presentation.css")
    assert re.search(
        r'\.deck-logo-grid\[data-equal-cells="true"\] \.deck-logo-cell \{\s*flex: 0 1 calc\(\(100% - \(var\(--logo-columns, 1\) - 1\) '
        r"\* var\(--logo-gap\)\) / var\(--logo-columns, 1\)\);",
        css,
    )
    assert 'data-align="even"] .deck-logo-cell' not in css
    logos = _read("blocks", "logos.tsx")
    assert "data-align={align}" in logos
    assert 'data-equal-cells={equalCells ? "true" : undefined}' in logos
    assert "const equalCells = logoGridEqualCells(block);" in logos
    assert '"--logo-columns": String(fullRowLength(rows, block.columns))' in logos
    sizing = _read("logo-sizing.ts")
    assert 'return block.align ?? (block.columns ? "even" : "stretch");' in sizing
    assert 'return align === "even" || (align === "start" && Boolean(block.columns));' in sizing
    # `start` packs every row from the start, short or full.
    assert '.deck-root .deck-logo-grid[data-align="start"] .deck-logo-row { justify-content: flex-start; }' in css


def test_a_fixed_tile_is_the_cell_height_and_the_bound_of_its_mark():
    """`tile_height`: the renderer writes the height as a custom
    property and the flags the stylesheet keys on; the cell takes the height,
    the mark is bounded by the cell's inner box through a width bound (the
    inner height times the mark's ratio, so the box keeps its ratio), and the
    balance is not capped in a fixed tile. A block without one matches none
    of it."""
    logos = _read("blocks", "logos.tsx")
    assert "if (!block.tile_height) return {};" in logos
    assert 'style: { "--logo-tile-h": `${block.tile_height}px` } as CSSProperties,' in logos
    assert logos.count('data-tile={tile["data-tile"]}') == 2  # the grid and the wall
    assert logos.count('data-labels={tile["data-labels"]}') == 2
    assert "maxBoost: logoMaxBoost(block)" in logos
    assert "logoHeightPx(shape, look.heightPx, look.balance, look.maxBoost)" in logos
    sizing = _read("logo-sizing.ts")
    assert "return block.tile_height ? Infinity : LOGO_BALANCE_MAX_BOOST;" in sizing
    assert "return Math.round(targetPx * clamp(area * ink, 1, maxBoost) * 10) / 10;" in sizing
    css = _read("presentation.css")
    assert re.search(
        r'\.deck-root \[data-tile="true"\] \.deck-logo-cell \{\s*box-sizing: border-box;\s*height: var\(--logo-tile-h\);',
        css,
    )
    assert re.search(
        r'\.deck-root \[data-tile="true"\] \.deck-logo-mark \{\s*'
        r"max-width: min\(100%, calc\(var\(--logo-cell-h\) \* var\(--logo-ratio, 1\)\)\);",
        css,
    )
    # The inner box: the tile less a framed tile's padding and 1 px border on each
    # side, and a shown label's line (without the border a mark at the full
    # inner height overflowed the framed cell by 2 px and pushed its label down).
    assert '.deck-root [data-tile="true"][data-framed="true"] { --logo-tile-pads: 1; }' in css
    assert '.deck-root [data-tile="true"][data-labels="true"] { --logo-label-lines: 1; }' in css
    assert "--logo-tile-border: 1px;" in css
    assert "border: var(--logo-tile-border) solid var(--deck-line);" in css
    assert (
        "var(--logo-tile-h) - var(--logo-tile-pads) * 2 * (var(--logo-tile-pad-y) + var(--logo-tile-border, 1px))"
        " - var(--logo-label-lines) * var(--logo-label-h)"
    ) in " ".join(css.split())
    # A hugging cell keeps the mark's historical bound alone.
    assert re.search(r"\.deck-root \.deck-logo-mark \{[^}]*max-width: 100%;", css)


def test_a_phases_timeline_shares_its_rows_and_a_logo_takes_the_fifth():
    """Every phases timeline is a grid of five shared rows - node, label,
    title, description, logo - its steps and their bodies subgrid into,
    so a wrapped title holds the title row for every step and the
    descriptions start on one line; a step's logo is the fifth row,
    `data-logos` still saying when one is drawn. The markup is unchanged, and
    an `events` timeline keeps its flex column."""
    flow = _read("blocks", "flow.tsx")
    assert 'const logos = style === "phases" && block.items.some((item) => item.logo_file_id);' in flow
    assert 'data-logos={logos ? "true" : undefined}' in flow
    css = _read("presentation.css")
    assert re.search(
        r'\.deck-timeline\[data-style="phases"\] \{[^}]*grid-template-rows: auto auto auto 1fr auto;',
        css,
    )
    assert re.search(
        r'\.deck-timeline\[data-style="phases"\] > \.deck-timeline-item \{[^}]*grid-row: span 5;[^}]*grid-template-rows: subgrid;',
        css,
    )
    assert re.search(
        r'\.deck-timeline\[data-style="phases"\] \.deck-timeline-body \{[^}]*grid-row: 2 / span 3;[^}]*grid-template-rows: subgrid;',
        css,
    )
    assert re.search(r'\.deck-timeline\[data-style="phases"\] \.deck-timeline-logo \{[^}]*grid-row: 5;', css)
    assert '[data-logos="true"]' not in css
    assert re.search(r'\.deck-timeline\[data-style="events"\] \{[^}]*flex-direction: column;', css)
