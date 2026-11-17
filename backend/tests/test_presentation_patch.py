"""Surgical presentation edits.

The complaint that opened the issue: to change one word on slide 12 of a
39-slide deck the agent had to re-submit the whole spec, a model never
re-emits the same 39 slides, and ``get_presentation`` cut the JSON at 20 000
chars so slides past ~15 could not even be read back. These tests pin the
contract that replaces that:

1. ``edit_presentation`` applies named operations to the STORED spec, in code,
   and every slide no operation names is byte-identical afterwards — verified
   by canonical JSON, not promised by a prompt.
2. The result says exactly which slides changed, were inserted, deleted or
   moved, and how many were left untouched.
3. Any slide of any deck can be read back in full: ``get_presentation`` never
   cuts mid-JSON, pages whole slides, and takes a ``slides`` selector.
4. Validation errors and permission refusals write nothing.

No DB required: the tool opens its own short-lived session, which is
monkeypatched with a capturing fake — same no-DB style as
``test_presentations.py``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import uuid
from pathlib import Path

import pytest

from app.services import presentation_patch as pp
from app.services.presentation_patch import (
    LAYOUT_DECK_FIELDS,
    MAX_OPERATIONS,
    OPERATION_CONTRACTS,
    OPERATIONS,
    PresentationPatchError,
    apply_operations,
    canonical,
    coerce_operations,
    coerce_revision,
    describe_operations,
    parse_slide_selection,
    summarize_patch,
    verify_untouched,
)
from app.services.presentation_spec import (
    DEFAULT_SLIDE_CAP,
    MAX_SLIDE_CAP,
    PresentationSpecError,
    validate_spec,
)

#: The deck chrome component: the one place the footer of every slide is drawn.
_CHROME_TSX = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation" / "chrome.tsx"

# ── Fixtures ─────────────────────────────────────────────────────────────────


def _deck(extra_slides: int = 0) -> dict:
    """A validated five-slide deck (ids assigned) — the baseline every test
    patches. ``extra_slides`` appends that many plain content slides."""
    slides: list[dict] = [
        {"layout": "title", "title": "Quarterly review", "subtitle": "Q3 2026"},
        {"layout": "agenda", "title": "Agenda", "items": ["Numbers", "Pipeline", "Plan"]},
        {
            "layout": "content",
            "title": "Numbers",
            "blocks": [
                {"type": "text", "content": "ARR grew 40% to 12 MEUR."},
                {"type": "list", "items": ["Churn 3%", "NRR 109%"]},
                {
                    "type": "callout",
                    "tone": "success",
                    "title": "Record quarter",
                    "children": [{"type": "text", "content": "Best Q3 so far."}],
                },
            ],
            "notes": "Say the ARR figure slowly.",
        },
        {
            "layout": "content",
            "title": "Pipeline",
            "columns": [
                [{"type": "text", "content": "EMEA strong"}],
                [{"type": "text", "content": "APAC building"}],
            ],
        },
        {"layout": "closing", "title": "Thanks"},
    ]
    for i in range(extra_slides):
        slides.append(
            {
                "layout": "content",
                "title": f"Extra {i + 1}",
                "blocks": [{"type": "text", "content": f"Filler paragraph {i + 1}. " * 20}],
            }
        )
    # The fixture is a READ of a deck, held to the ceiling: the
    # cap tests build decks longer than the platform default on purpose.
    return validate_spec({"title": "Quarterly review", "slides": slides}, slide_cap=MAX_SLIDE_CAP)


def _ids(spec: dict) -> list[str]:
    return [s["id"] for s in spec["slides"]]


def _by_id(spec: dict) -> dict[str, dict]:
    return {s["id"]: s for s in spec["slides"]}


def _assert_untouched_identical(baseline: dict, patched: dict, touched: set[str]) -> None:
    """The guarantee itself, stated twice: the helper the tool runs, and a
    literal canonical comparison so the helper cannot be trivially green."""
    assert verify_untouched(baseline, patched, touched) == []
    before, after = _by_id(baseline), _by_id(patched)
    for sid, slide in before.items():
        if sid in touched or sid not in after:
            continue
        assert canonical(slide) == canonical(after[sid]), sid


# ── The engine: identity of untouched slides ─────────────────────────────────


def test_the_baseline_normalisation_is_idempotent():
    """The tool validates the stored spec to get its baseline, then validates
    the patched spec: if validation were not a fixed point, normalisation
    would show up as a "change" on every slide."""
    spec = _deck()
    assert canonical(validate_spec(spec)) == canonical(spec)
    assert _ids(validate_spec(spec)) == _ids(spec)


def test_replacing_one_word_changes_exactly_one_slide():
    spec = _deck()
    out = apply_operations(
        spec, [{"op": "replace_text", "slide": 3, "find": "12 MEUR", "replace": "12.4 MEUR"}]
    )
    patched = validate_spec(out.spec)
    assert [r.index for r in out.changed] == [3]
    assert out.unchanged_count == 4
    assert out.replacements == 1
    assert patched["slides"][2]["blocks"][0]["content"] == "ARR grew 40% to 12.4 MEUR."
    _assert_untouched_identical(spec, patched, out.touched_ids)
    assert summarize_patch(out) == "Edited slide 3; 1 text replacement"


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "update_slide", "slide": 1, "fields": {"subtitle": "Q3 2026 - board edition"}},
        {"op": "replace_slide", "slide": 5, "content": {"layout": "closing", "title": "Merci"}},
        {"op": "insert_slide", "after": 2, "content": {"layout": "section", "title": "Part one"}},
        {"op": "delete_slide", "slide": 2},
        {"op": "move_slide", "slide": 5, "to": 2},
        {"op": "update_block", "slide": 3, "block": "2", "fields": {"items": ["Churn 2.8%"]}},
        {"op": "replace_block", "slide": 3, "block": "1", "content": {"type": "quote", "content": "Wow", "attribution": "CFO"}},
        {"op": "insert_block", "slide": 3, "content": {"type": "text", "content": "Appended."}},
        {"op": "delete_block", "slide": 3, "block": "3"},
        {"op": "replace_text", "find": "Pipeline", "replace": "Sales pipeline", "count": 2},
        {"op": "update_deck", "fields": {"title": "Q3 review", "chrome": {"show_footer": False}}},
    ],
    ids=lambda op: op["op"],
)
def test_every_operation_leaves_the_slides_it_does_not_name_byte_identical(operation):
    spec = _deck()
    frozen = copy.deepcopy(spec)
    out = apply_operations(spec, [operation])
    patched = validate_spec(out.spec)
    _assert_untouched_identical(spec, patched, out.touched_ids)
    # The engine works on a copy: the caller's baseline is never mutated.
    assert canonical(spec) == canonical(frozen)
    # And the accounting is honest: whatever the op, at most the slides it
    # named (plus an insert) differ from the baseline.
    before, after = _by_id(spec), _by_id(patched)
    drifted = {sid for sid in before if sid in after and canonical(before[sid]) != canonical(after[sid])}
    assert drifted <= set(out.touched_ids)
    assert set(r.id for r in out.inserted) == set(after) - set(before)


def test_untouched_slides_survive_a_burst_of_mixed_operations():
    """The realistic case: several edits in one call, positions shifting
    under each other (an insert, then a move by NEW position, then a delete)."""
    spec = _deck()
    out = apply_operations(
        spec,
        [
            {"op": "update_block", "slide": 3, "block": "2", "fields": {"items": ["Churn 2.8%", "NRR 109%"]}},
            {"op": "update_slide", "slide": "1", "fields": {"subtitle": "Board edition"}},
            {"op": "insert_slide", "after": 2, "content": {"layout": "section", "title": "Part one"}},
            {"op": "move_slide", "slide": 5, "to": 3},
            {"op": "delete_slide", "slide": 6},
        ],
    )
    patched = validate_spec(out.spec)
    _assert_untouched_identical(spec, patched, out.touched_ids)
    assert [s["title"] for s in patched["slides"]] == [
        "Quarterly review",
        "Agenda",
        "Pipeline",
        "Part one",
        "Numbers",
    ]
    assert sorted(r.index for r in out.changed) == [1, 5]
    assert [(r.index, r.title) for r in out.inserted] == [(4, "Part one")]
    assert [(r.index, old, r.title) for r, old in out.moved] == [(3, 4, "Pipeline")]
    assert [(r.index, r.title) for r in out.deleted] == [(5, "Thanks")]
    # Agenda (slide 2) was never named and never moved: it is the ONE slide
    # both untouched and in place; Pipeline moved but its content is intact.
    assert out.unchanged_count == 2
    assert summarize_patch(out) == (
        "Edited slides 1, 5; Inserted slide 4; Deleted slide 5; Moved slide 4 to 3"
    )
    # The history line follows the kind: a portrait page never says "slide".
    page = dict(out.spec, aspect_ratio="A4")
    assert summarize_patch(out, page) == (
        "Edited pages 1, 5; Inserted page 4; Deleted page 5; Moved page 4 to 3"
    )
    assert summarize_patch(out, out.spec) == summarize_patch(out)


def test_an_edit_carries_at_most_max_operations():
    """A burst past the cap is a rewrite in disguise; it is refused before
    anything is applied, with the way out named."""
    spec = _deck()
    ops = [{"op": "update_deck", "fields": {"title": f"Title {i}"}} for i in range(MAX_OPERATIONS)]
    assert apply_operations(spec, ops).spec["title"] == f"Title {MAX_OPERATIONS - 1}"
    with pytest.raises(PresentationPatchError, match=rf"{MAX_OPERATIONS + 1} operations.*limit is {MAX_OPERATIONS}"):
        apply_operations(spec, [*ops, {"op": "update_deck", "fields": {"title": "One too many"}}])


# ── Addressing ───────────────────────────────────────────────────────────────


def test_a_slide_is_addressed_by_position_or_by_id():
    spec = _deck()
    target = spec["slides"][3]["id"]
    by_pos = apply_operations(spec, [{"op": "update_slide", "slide": 4, "fields": {"title": "Deals"}}])
    by_str = apply_operations(spec, [{"op": "update_slide", "slide": "4", "fields": {"title": "Deals"}}])
    by_id = apply_operations(spec, [{"op": "update_slide", "slide": target, "fields": {"title": "Deals"}}])
    assert canonical(by_pos.spec) == canonical(by_str.spec) == canonical(by_id.spec)
    assert by_id.changed[0].id == target
    for bad in (0, 6, "nope", -1):
        with pytest.raises(PresentationPatchError, match=r"does not exist|no slide has id"):
            apply_operations(spec, [{"op": "update_slide", "slide": bad, "fields": {"title": "x"}}])


def test_block_addresses_cover_columns_footer_and_nested_blocks():
    spec = _deck()
    out = apply_operations(
        spec,
        [
            # column 2, block 1 of the columns slide
            {"op": "update_block", "slide": 4, "block": "2.1", "fields": {"content": "APAC accelerating"}},
            # the footer slot
            {
                "op": "insert_block",
                "slide": 4,
                "at": "footer",
                "content": {"type": "callout", "tone": "info", "title": "Note", "content": "As of Sept."},
            },
            # first child of the callout (block 3 of slide 3)
            {"op": "update_block", "slide": 3, "block": "3.1", "fields": {"content": "Best quarter ever."}},
        ],
    )
    patched = validate_spec(out.spec)
    assert patched["slides"][3]["columns"][1][0]["content"] == "APAC accelerating"
    assert patched["slides"][3]["footer_block"]["type"] == "callout"
    assert patched["slides"][2]["blocks"][2]["children"][0]["content"] == "Best quarter ever."
    _assert_untouched_identical(spec, patched, out.touched_ids)

    # A bare position on a columns slide is ambiguous and says so.
    with pytest.raises(PresentationPatchError, match=r"column\.position"):
        apply_operations(spec, [{"op": "update_block", "slide": 4, "block": "1", "fields": {"content": "x"}}])
    # A footer that exists is not silently overwritten by an insert.
    with pytest.raises(PresentationPatchError, match="footer"):
        apply_operations(
            validate_spec(patched),
            [{"op": "insert_block", "slide": 4, "at": "footer", "content": {"type": "text", "content": "x"}}],
        )
    # An insert on a columns slide needs a column.
    with pytest.raises(PresentationPatchError, match="columns slide"):
        apply_operations(spec, [{"op": "insert_block", "slide": 4, "content": {"type": "text", "content": "x"}}])
    # A layout with no body blocks says so instead of inventing a list, and
    # names the one block it can take when it has one.
    with pytest.raises(PresentationPatchError, match="has no body blocks") as exc:
        apply_operations(spec, [{"op": "insert_block", "slide": 1, "content": {"type": "text", "content": "x"}}])
    assert "footer" not in str(exc.value)
    with pytest.raises(PresentationPatchError, match=r"has no body blocks.*block 'footer'"):
        apply_operations(spec, [{"op": "update_block", "slide": 2, "block": "1", "fields": {"content": "x"}}])


def test_a_columns_block_is_addressed_block_column_position():
    """Inside a ``columns`` BLOCK the columns are lists, so a contained block
    takes three segments (``'2.2.1'``); ``'2.2'`` alone names the column and is
    refused by every block operation instead of corrupting the structure."""
    spec = validate_spec(
        {
            "title": "Deck",
            "slides": [
                {
                    "layout": "content",
                    "title": "Regions",
                    "blocks": [
                        {"type": "text", "content": "Two regions."},
                        {
                            "type": "columns",
                            "columns": [
                                [{"type": "text", "content": "EMEA strong"}],
                                [{"type": "text", "content": "APAC building"}, {"type": "text", "content": "LATAM new"}],
                            ],
                        },
                    ],
                }
            ],
        }
    )
    out = apply_operations(
        spec,
        [
            {"op": "update_block", "slide": 1, "block": "2.2.1", "fields": {"content": "APAC accelerating"}},
            {"op": "insert_block", "slide": 1, "at": "2.1.2", "content": {"type": "text", "content": "UK steady"}},
            {"op": "delete_block", "slide": 1, "block": "2.2.2"},
        ],
    )
    columns = validate_spec(out.spec)["slides"][0]["blocks"][1]["columns"]
    assert [b["content"] for b in columns[0]] == ["EMEA strong", "UK steady"]
    assert [b["content"] for b in columns[1]] == ["APAC accelerating"]
    for op in (
        {"op": "update_block", "slide": 1, "block": "2.2", "fields": {"content": "x"}},
        {"op": "replace_block", "slide": 1, "block": "2.2", "content": {"type": "text", "content": "x"}},
        {"op": "delete_block", "slide": 1, "block": "2.1"},
        {"op": "insert_block", "slide": 1, "at": "2.3", "content": {"type": "text", "content": "x"}},
    ):
        with pytest.raises(PresentationPatchError, match=r"names a column of a columns block.*'2\.[123]\.1'"):
            apply_operations(spec, [op])


def test_a_comparison_panel_is_addressed_by_side_then_position():
    """``'N.left.M'`` / ``'N.right.M'`` step into a comparison block's panels;
    a bare side or a numeric segment where a side is expected is refused."""
    spec = validate_spec(
        {
            "title": "Deck",
            "slides": [
                {
                    "layout": "content",
                    "title": "Before / after",
                    "blocks": [
                        {
                            "type": "comparison",
                            "left": {"label": "Before", "blocks": [{"type": "text", "content": "Manual triage"}]},
                            "right": {"label": "After", "blocks": [{"type": "text", "content": "Automated triage"}]},
                        }
                    ],
                }
            ],
        }
    )
    out = apply_operations(
        spec,
        [{"op": "update_block", "slide": 1, "block": "1.right.1", "fields": {"content": "Triage in seconds"}}],
    )
    block = out.spec["slides"][0]["blocks"][0]
    assert block["right"]["blocks"][0]["content"] == "Triage in seconds"
    assert block["left"]["blocks"][0]["content"] == "Manual triage"
    with pytest.raises(PresentationPatchError, match="'left' or 'right'"):
        apply_operations(spec, [{"op": "delete_block", "slide": 1, "block": "1.2"}])
    with pytest.raises(PresentationPatchError, match="names a comparison panel"):
        apply_operations(spec, [{"op": "delete_block", "slide": 1, "block": "1.left"}])


def _dashboard_deck() -> dict:
    """A title slide and a three-panel dashboard: a span-2 bar chart
    and a donut on the first row, a table on the second."""
    bars = {"type": "chart", "kind": "bar", "categories": ["Q1", "Q2", "Q3"], "series": [{"name": "ARR", "values": [4, 5, 7]}]}
    mix = {"type": "chart", "kind": "donut", "categories": ["EE", "CE"], "series": [{"name": "Mix", "values": [40, 60]}]}
    table = {"type": "table", "headers": ["Region", "ARR"], "rows": [["EMEA", "7"], ["APAC", "3"]]}
    return validate_spec(
        {
            "title": "Board review",
            "slides": [
                {"layout": "title", "title": "Board review"},
                {
                    "layout": "dashboard",
                    "title": "ARR keeps compounding",
                    "tiles": [{"value": "EUR 7M", "label": "ARR"}, {"value": "112", "label": "Trials"}],
                    "panels": [
                        {"title": "ARR per quarter", "span": 2, "block": bars},
                        {"title": "Edition mix", "block": mix},
                        {"title": "By region", "block": table},
                    ],
                },
            ],
        }
    )


def test_a_dashboard_panel_is_block_n_and_its_neighbours_stay_byte_identical():
    """The 19 Oct 2026 run: a layout-check remedy on a dashboard
    chart is one update_block on its panel - "a dashboard slide has no blocks"
    sent the agent to re-send every panel with all its data. Block 'N' is
    panel N: the figure takes its own fields, the panel its title and span
    (a panel's chart carries no title of its own), and the other panels, the
    tiles and the other slides are untouched."""
    spec = _dashboard_deck()
    before = spec["slides"][1]
    out = apply_operations(
        spec,
        [
            {
                "op": "update_block",
                "slide": "2",
                "block": "1",
                "fields": {"value_label_fit": "stagger", "legend_position": "bottom", "title": "ARR by quarter", "span": 1},
            }
        ],
    )
    patched = validate_spec(out.spec)
    panels = patched["slides"][1]["panels"]
    assert (panels[0]["title"], panels[0]["span"]) == ("ARR by quarter", 1)
    assert panels[0]["block"]["value_label_fit"] == "stagger"
    assert panels[0]["block"]["legend_position"] == "bottom"
    assert "title" not in panels[0]["block"]
    for index in (1, 2):
        assert canonical(panels[index]) == canonical(before["panels"][index]), index
    assert canonical(patched["slides"][1]["tiles"]) == canonical(before["tiles"])
    assert [ref.index for ref in out.changed] == [2]
    _assert_untouched_identical(spec, patched, out.touched_ids)

    # A null removes a panel field like any other field.
    untitled = validate_spec(
        apply_operations(spec, [{"op": "update_block", "slide": "2", "block": "2", "fields": {"title": None}}]).spec
    )
    assert "title" not in untitled["slides"][1]["panels"][1]
    # The footer of a dashboard is still block 'footer'.
    footed = apply_operations(
        spec,
        [{"op": "insert_block", "slide": "2", "at": "footer", "content": {"type": "callout", "content": "Plan beaten."}}],
    )
    assert validate_spec(footed.spec)["slides"][1]["footer_block"]["content"] == "Plan beaten."


def test_a_dashboard_panel_is_replaced_added_and_removed_in_place():
    """replace_block swaps a panel's figure under its title, insert_block adds
    a panel holding the block (titled in the same call by its address),
    delete_block removes the panel with its figure; an address inside a
    figure, past the panels or nested in an insert is refused by name."""
    spec = _dashboard_deck()
    trials = {"type": "chart", "kind": "bar", "categories": ["Q1", "Q2"], "series": [{"name": "Trials", "values": [91, 112]}]}

    out = apply_operations(spec, [{"op": "replace_block", "slide": "2", "block": "3", "content": trials}])
    third = validate_spec(out.spec)["slides"][1]["panels"][2]
    assert (third["title"], third["block"]["type"]) == ("By region", "chart")

    out = apply_operations(
        spec,
        [
            {"op": "insert_block", "slide": "2", "at": "2", "content": trials},
            {"op": "update_block", "slide": "2", "block": "2", "fields": {"title": "Trials"}},
            {"op": "delete_block", "slide": "2", "block": "3"},
        ],
    )
    panels = validate_spec(out.spec)["slides"][1]["panels"]
    assert [panel.get("title") for panel in panels] == ["ARR per quarter", "Trials", "By region"]
    assert panels[1]["span"] == 1
    _assert_untouched_identical(spec, out.spec, out.touched_ids)

    appended = apply_operations(spec, [{"op": "insert_block", "slide": "2", "content": trials}])
    assert len(appended.spec["slides"][1]["panels"]) == 4

    for op, message in (
        ({"op": "update_block", "slide": "2", "block": "1.1", "fields": {"x": 1}}, r"block '1' \(chart\) has no nested blocks"),
        ({"op": "delete_block", "slide": "2", "block": "4"}, r"panel 4 does not exist: the dashboard has 3 panel"),
        ({"op": "insert_block", "slide": "2", "at": "1.2", "content": trials}, "a block is added as a panel"),
        ({"op": "insert_block", "slide": "2", "at": "5", "content": trials}, "or 4 to add one at the end"),
    ):
        with pytest.raises(PresentationPatchError, match=message):
            apply_operations(spec, [op])

    # A chart title sent with a replacement figure is the validator's to
    # refuse, as on a fresh deck: the panel titles its chart.
    titled = apply_operations(
        spec, [{"op": "replace_block", "slide": "2", "block": "1", "content": {**trials, "title": "Trials"}}]
    )
    with pytest.raises(PresentationSpecError, match="titled by the panel"):
        validate_spec(titled.spec)


def test_update_slide_and_update_block_keep_identity_and_type():
    spec = _deck()
    with pytest.raises(PresentationPatchError, match="'layout' cannot be changed"):
        apply_operations(spec, [{"op": "update_slide", "slide": 1, "fields": {"layout": "section"}}])
    with pytest.raises(PresentationPatchError, match="'id' cannot be changed"):
        apply_operations(spec, [{"op": "update_slide", "slide": 1, "fields": {"id": "abc"}}])
    with pytest.raises(PresentationPatchError, match="'type' cannot be changed"):
        apply_operations(spec, [{"op": "update_block", "slide": 3, "block": "1", "fields": {"type": "list"}}])
    # A null value removes a field.
    out = apply_operations(spec, [{"op": "update_slide", "slide": 3, "fields": {"notes": None}}])
    assert "notes" not in out.spec["slides"][2]
    # replace_slide keeps the slide's id so anchored comments stay.
    out = apply_operations(
        spec, [{"op": "replace_slide", "slide": 3, "content": {"layout": "section", "title": "Numbers"}}]
    )
    assert out.spec["slides"][2]["id"] == spec["slides"][2]["id"]
    assert out.spec["slides"][2]["layout"] == "section"


# ── replace_text ─────────────────────────────────────────────────────────────


def test_replace_text_is_exact_scoped_and_refuses_ambiguity():
    from app.services.presentation_patch import PresentationPatchNoChangeError

    spec = _deck()
    # Absent text is named, never a silent no-op - and it is the NO-CHANGE
    # kind of refusal: the tool answers it as "No change", not as a
    # failure the loop's brake counts.
    with pytest.raises(PresentationPatchNoChangeError, match="was not found"):
        apply_operations(spec, [{"op": "replace_text", "find": "nope", "replace": "x"}])
    # Case-sensitive.
    with pytest.raises(PresentationPatchNoChangeError, match="was not found"):
        apply_operations(spec, [{"op": "replace_text", "find": "arr grew", "replace": "x"}])
    assert issubclass(PresentationPatchNoChangeError, PresentationPatchError)
    # Scoped to one slide: "Numbers" appears in the agenda AND as slide 3's
    # title; the scope keeps the agenda untouched.
    out = apply_operations(spec, [{"op": "replace_text", "slide": 3, "find": "Numbers", "replace": "Figures"}])
    assert [r.index for r in out.changed] == [3]
    assert out.spec["slides"][1]["items"][0]["title"] == "Numbers"
    # Unscoped with a wrong count is refused and the per-slide counts are listed.
    with pytest.raises(PresentationPatchError, match="found 2") as exc:
        apply_operations(spec, [{"op": "replace_text", "find": "Numbers", "replace": "Figures", "count": 1}])
    assert "slide 2" in str(exc.value) and "slide 3" in str(exc.value)
    # Unscoped with NO count and several occurrences is refused too - the
    # blind deck-wide edit the tool exists to prevent - and says where they are.
    with pytest.raises(PresentationPatchError, match="occurs 2 times") as exc:
        apply_operations(spec, [{"op": "replace_text", "find": "Numbers", "replace": "Figures"}])
    assert "slide 2 (1), slide 3 (1)" in str(exc.value) and "count=2" in str(exc.value)
    # Unscoped with the right count touches both, on purpose.
    out = apply_operations(spec, [{"op": "replace_text", "find": "Numbers", "replace": "Figures", "count": 2}])
    assert [r.index for r in out.changed] == [2, 3]
    assert out.replacements == 2
    # Scoped to one slide, several occurrences on THAT slide need no count:
    # the slide is the pointer.
    out = apply_operations(spec, [{"op": "replace_text", "slide": 3, "find": "%", "replace": " pct"}])
    assert out.replacements == 3 and [r.index for r in out.changed] == [3]
    # The deck title is text too (unscoped), and counts as a deck field; it
    # is listed with the slides when the phrase is in both.
    with pytest.raises(PresentationPatchError, match="deck title"):
        apply_operations(spec, [{"op": "replace_text", "find": "Quarterly", "replace": "Q3"}])
    out = apply_operations(spec, [{"op": "replace_text", "find": "Quarterly", "replace": "Q3", "count": 2}])
    assert out.spec["title"] == "Q3 review" and out.spec["slides"][0]["title"] == "Q3 review"
    assert "title" in out.deck_fields
    # A single unscoped occurrence needs no count.
    out = apply_operations(spec, [{"op": "replace_text", "find": "EMEA strong", "replace": "EMEA ahead"}])
    assert out.replacements == 1 and [r.index for r in out.changed] == [4]
    # An empty replacement deletes the text; an empty find is refused.
    out = apply_operations(spec, [{"op": "replace_text", "slide": 3, "find": " to 12 MEUR", "replace": ""}])
    assert out.spec["slides"][2]["blocks"][0]["content"] == "ARR grew 40%."
    with pytest.raises(PresentationPatchError, match="'find'"):
        apply_operations(spec, [{"op": "replace_text", "find": "", "replace": "x"}])


def test_a_delta_tone_is_set_in_place_and_never_rewritten_as_text():
    """On a stored deck: one tile's delta gets its tone, the
    rest of the deck is untouched, and a word that happens to be a tone value
    is replaced in the text only - never in the field, which would then fail
    validation and lose the whole edit."""
    tiles = [
        {"value": "EUR 1.7M", "label": "Loss, Q3", "sublabel": "success in cost control", "delta": "-18.4% QoQ", "trend": "down"},
        {"value": "1.27x", "label": "Burn multiple"},
    ]
    spec = validate_spec(
        {
            "title": "Quarterly Review, Q3 2026",
            "slides": [
                {"layout": "title", "title": "Q3 2026"},
                {"layout": "content", "title": "Q3 at a glance", "blocks": [{"type": "metrics_grid", "items": tiles}]},
            ],
        }
    )
    toned = [{**tiles[0], "delta_tone": "success"}, tiles[1]]
    out = apply_operations(spec, [{"op": "update_block", "slide": 2, "block": "1", "fields": {"items": toned}}])
    assert [r.index for r in out.changed] == [2]
    _assert_untouched_identical(spec, out.spec, {spec["slides"][1]["id"]})
    saved = validate_spec(out.spec)
    assert saved["slides"][1]["blocks"][0]["items"][0]["delta_tone"] == "success"
    renamed = apply_operations(saved, [{"op": "replace_text", "slide": 2, "find": "success", "replace": "progress"}])
    tile = renamed.spec["slides"][1]["blocks"][0]["items"][0]
    assert (tile["sublabel"], tile["delta_tone"]) == ("progress in cost control", "success")
    validate_spec(renamed.spec)


def test_a_dashboard_tile_size_is_set_in_place_and_never_rewritten_as_text():
    """On a stored deck: one dashboard takes the small tile row through
    update_slide, the rest of the deck is untouched, and a word equal to a
    size is replaced in the text only - never in the field."""
    tiles = [
        {"value": "EUR 12.3M", "label": "Region A ARR", "sublabel": "Plan: sm tier"},
        {"value": "EUR 7.8M", "label": "Region B ARR"},
    ]
    chart = {"type": "chart", "kind": "bar", "categories": ["Q2", "Q3"], "series": [{"name": "ARR", "values": [6.7, 12.3]}]}
    spec = validate_spec(
        {
            "title": "Regional ARR, Q3 2026",
            "slides": [
                {"layout": "title", "title": "Q3 2026"},
                {"layout": "dashboard", "title": "Regional ARR", "tiles": tiles, "panels": [{"title": "ARR", "block": chart}]},
            ],
        }
    )
    assert spec["slides"][1]["tile_size"] == "md"
    out = apply_operations(spec, [{"op": "update_slide", "slide": 2, "fields": {"tile_size": "sm"}}])
    assert [r.index for r in out.changed] == [2]
    _assert_untouched_identical(spec, out.spec, {spec["slides"][1]["id"]})
    saved = validate_spec(out.spec)
    assert saved["slides"][1]["tile_size"] == "sm"
    renamed = apply_operations(saved, [{"op": "replace_text", "slide": 2, "find": "sm", "replace": "small"}])
    slide = renamed.spec["slides"][1]
    assert (slide["tiles"][0]["sublabel"], slide["tile_size"]) == ("Plan: small tier", "sm")
    validate_spec(renamed.spec)


def test_replace_text_never_touches_structural_strings():
    """Layouts, block types, tones, ids and file ids are strings too: a find
    that happens to equal one is not found at all, so structure can never be
    rewritten by a text replacement."""
    file_id = str(uuid.uuid4())
    spec = validate_spec(
        {
            "title": "Deck",
            "slides": [
                {
                    "layout": "content",
                    "title": "Diagram",
                    "blocks": [
                        {"type": "image", "file_id": file_id},
                        {"type": "callout", "tone": "success", "title": "Win"},
                    ],
                }
            ],
        }
    )
    for find in ("content", "image", "callout", "success", file_id, spec["slides"][0]["id"]):
        with pytest.raises(PresentationPatchError, match="was not found"):
            apply_operations(spec, [{"op": "replace_text", "find": find, "replace": "ZZZ"}])


# ── Structure: insert / delete / move / deck ─────────────────────────────────


def test_insert_slide_positions_and_bounds():
    spec = _deck()
    section = {"layout": "section", "title": "Part one"}
    appended = apply_operations(spec, [{"op": "insert_slide", "content": section}])
    assert appended.spec["slides"][-1]["title"] == "Part one"
    at = apply_operations(spec, [{"op": "insert_slide", "at": 1, "content": section}])
    assert at.spec["slides"][0]["title"] == "Part one"
    assert at.inserted[0].index == 1
    after = apply_operations(spec, [{"op": "insert_slide", "after": spec["slides"][1]["id"], "content": section}])
    assert after.spec["slides"][2]["title"] == "Part one"
    # The new slide has a fresh id nobody else carries.
    new_id = after.inserted[0].id
    assert new_id and new_id not in _ids(spec)
    assert _ids(after.spec).count(new_id) == 1
    with pytest.raises(PresentationPatchError, match="not both"):
        apply_operations(spec, [{"op": "insert_slide", "at": 1, "after": 2, "content": section}])
    full = _deck(extra_slides=DEFAULT_SLIDE_CAP - 5)
    assert len(full["slides"]) == DEFAULT_SLIDE_CAP
    # The refusal names the cap, says it is the template's and how it is
    # raised: a deck at its cap is not a deck at the platform's limit.
    with pytest.raises(PresentationPatchError) as exc:
        apply_operations(full, [{"op": "insert_slide", "content": section}])
    refusal = str(exc.value)
    assert f"deck already has {DEFAULT_SLIDE_CAP} slides, its cap of {DEFAULT_SLIDE_CAP}" in refusal
    assert "delete or merge a slide" in refusal
    assert "max_slides" in refusal
    # One under the cap still takes a slide: the cap is a bound, not a budget.
    almost = _deck(extra_slides=DEFAULT_SLIDE_CAP - 6)
    assert len(almost["slides"]) == DEFAULT_SLIDE_CAP - 1
    assert (
        len(apply_operations(almost, [{"op": "insert_slide", "content": section}]).spec["slides"])
        == DEFAULT_SLIDE_CAP
    )


def test_insert_slide_takes_the_decks_own_cap():
    """The cap is the template's: the SAME full deck takes a slide
    under a higher ``slide_cap`` and is refused at that cap in turn; a deck
    already over its cap is refused at its size, with the cap it is over."""
    section = {"layout": "section", "title": "Part one"}
    full = _deck(extra_slides=DEFAULT_SLIDE_CAP - 5)
    with pytest.raises(PresentationPatchError, match="its cap of"):
        apply_operations(full, [{"op": "insert_slide", "content": section}])
    grown = apply_operations(full, [{"op": "insert_slide", "content": section}], slide_cap=150)
    assert len(grown.spec["slides"]) == DEFAULT_SLIDE_CAP + 1
    at_150 = _deck(extra_slides=145)
    with pytest.raises(PresentationPatchError) as exc:
        apply_operations(at_150, [{"op": "insert_slide", "content": section}], slide_cap=150)
    assert "deck already has 150 slides, its cap of 150" in str(exc.value)
    # Over the cap (the template was tightened since): the number named is
    # the cap, not the size, so the agent is not told 140 is the limit.
    over = _deck(extra_slides=135)
    with pytest.raises(PresentationPatchError) as exc:
        apply_operations(over, [{"op": "insert_slide", "content": section}], slide_cap=DEFAULT_SLIDE_CAP)
    assert f"deck already has 140 slides, its cap of {DEFAULT_SLIDE_CAP}" in str(exc.value)
    # At the ceiling the refusal says there is nothing to raise.
    ceiling = _deck(extra_slides=MAX_SLIDE_CAP - 5)
    with pytest.raises(PresentationPatchError) as exc:
        apply_operations(ceiling, [{"op": "insert_slide", "content": section}], slide_cap=MAX_SLIDE_CAP * 2)
    assert "platform ceiling" in str(exc.value)
    # Every other operation is indifferent to the cap: an over-cap deck is
    # still edited in place.
    edited = apply_operations(
        over, [{"op": "update_slide", "slide": 1, "fields": {"title": "Renamed"}}], slide_cap=DEFAULT_SLIDE_CAP
    )
    assert edited.spec["slides"][0]["title"] == "Renamed"


def test_delete_and_move_are_accounted_by_their_original_position():
    spec = _deck()
    out = apply_operations(spec, [{"op": "delete_slide", "slide": 2}])
    assert [(r.index, r.title) for r in out.deleted] == [(2, "Agenda")]
    assert out.changed == [] and out.unchanged_count == 4
    assert spec["slides"][1]["id"] in out.touched_ids
    out = apply_operations(spec, [{"op": "move_slide", "slide": 5, "to": 1}])
    assert [(r.index, old) for r, old in out.moved] == [(1, 5)]
    assert out.changed == []
    # Moving a slide onto its own position is a no-op, not a move.
    out = apply_operations(spec, [{"op": "move_slide", "slide": 2, "to": 2}])
    assert out.moved == [] and out.is_noop
    assert canonical(out.spec) == canonical(spec)
    with pytest.raises(PresentationPatchError, match="only slide"):
        apply_operations(validate_spec({"title": "One", "slides": [spec["slides"][0]]}), [{"op": "delete_slide", "slide": 1}])


def test_update_deck_merges_chrome_and_refuses_unknown_fields():
    spec = validate_spec({**_deck(), "chrome": {"show_logo": True, "footer_text": "Filigran"}})
    out = apply_operations(spec, [{"op": "update_deck", "fields": {"chrome": {"show_footer": False}, "theme": "light"}}])
    patched = validate_spec(out.spec)
    assert patched["chrome"]["footer_text"] == "Filigran"  # merged, not replaced
    assert patched["chrome"]["show_footer"] is False
    assert patched["theme"] == "light"
    assert sorted(out.deck_fields) == ["chrome", "theme"]
    assert out.changed == [] and out.unchanged_count == 5
    _assert_untouched_identical(spec, patched, out.touched_ids)
    with pytest.raises(PresentationPatchError, match="cannot set slides"):
        apply_operations(spec, [{"op": "update_deck", "fields": {"slides": []}}])
    with pytest.raises(PresentationPatchError, match="bogus"):
        apply_operations(spec, [{"op": "update_deck", "fields": {"bogus": 1}}])


def test_update_deck_moves_the_logo_without_touching_the_rest_of_the_chrome():
    """The logo placement is one more chrome key: merged key by key,
    validated like a fresh deck, and a deck-level field drawn on every slide."""
    spec = validate_spec({**_deck(), "chrome": {"footer_text": "Filigran", "show_slide_numbers": False}})
    assert spec["chrome"]["logo_position"] == "auto"
    out = apply_operations(spec, [{"op": "update_deck", "fields": {"chrome": {"logo_position": "center"}}}])
    patched = validate_spec(out.spec)
    assert patched["chrome"]["logo_position"] == "center"
    assert patched["chrome"]["footer_text"] == "Filigran"
    assert patched["chrome"]["show_slide_numbers"] is False
    assert out.deck_fields == ["chrome"]
    _assert_untouched_identical(spec, patched, out.touched_ids)
    # Setting it back to the default is a real change of the stored value.
    back = apply_operations(patched, [{"op": "update_deck", "fields": {"chrome": {"logo_position": "auto"}}}])
    assert validate_spec(back.spec)["chrome"]["logo_position"] == "auto"
    # A placement the renderer does not know is refused before anything is saved.
    with pytest.raises(PresentationSpecError, match="logo_position"):
        validate_spec(
            apply_operations(spec, [{"op": "update_deck", "fields": {"chrome": {"logo_position": "bottom-right"}}}]).spec
        )


def test_a_named_but_unchanged_slide_is_reported_as_such():
    spec = _deck()
    out = apply_operations(spec, [{"op": "update_slide", "slide": 5, "fields": {"title": "Thanks"}}])
    assert out.changed == []
    assert [r.index for r in out.named_unchanged] == [5]
    assert out.is_noop
    assert summarize_patch(out) == "No change"


# ── Errors and coercion ──────────────────────────────────────────────────────


def test_errors_name_the_failing_operation():
    spec = _deck()
    with pytest.raises(PresentationPatchError, match=r"^operation 2 \(delete_slide\): slide 99 does not exist"):
        apply_operations(
            spec,
            [
                {"op": "update_slide", "slide": 1, "fields": {"title": "ok"}},
                {"op": "delete_slide", "slide": 99},
            ],
        )
    with pytest.raises(PresentationPatchError, match="operation 1: unknown op 'explode'"):
        apply_operations(spec, [{"op": "explode"}])
    with pytest.raises(PresentationPatchError, match="operation 1 has no 'op' key"):
        apply_operations(spec, [{"slide": 1}])


def test_coerce_operations_accepts_a_json_string_or_a_single_object():
    ops = [{"op": "delete_slide", "slide": 2}]
    assert coerce_operations(ops) == ops
    assert coerce_operations(json.dumps(ops)) == ops
    assert coerce_operations(ops[0]) == ops
    assert coerce_operations(json.dumps(ops[0])) == ops
    for bad in ("", "not json", [], [1], None, 3):
        with pytest.raises(PresentationPatchError):
            coerce_operations(bad)


def test_the_patched_spec_is_validated_like_a_fresh_deck():
    """The engine does not know the spec contract; validation does. An invented
    field is refused with the slide addressed — and nothing else is touched."""
    spec = _deck()
    out = apply_operations(spec, [{"op": "update_slide", "slide": 3, "fields": {"bogus_field": 1}}])
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(out.spec)
    assert "slides.2" in str(exc.value)


# ── The verifier ─────────────────────────────────────────────────────────────


def test_verify_untouched_reports_a_drifted_or_vanished_slide():
    spec = _deck()
    drifted = copy.deepcopy(spec)
    drifted["slides"][1]["title"] = "Agenda (rewritten)"
    problems = verify_untouched(spec, drifted, set())
    assert len(problems) == 1 and "slide 2" in problems[0]
    # Naming it makes the drift legitimate.
    assert verify_untouched(spec, drifted, {spec["slides"][1]["id"]}) == []
    vanished = copy.deepcopy(spec)
    del vanished["slides"][4]
    assert any("disappeared" in p for p in verify_untouched(spec, vanished, set()))
    assert verify_untouched(spec, vanished, {spec["slides"][4]["id"]}) == []


# ── Selection and catalog ────────────────────────────────────────────────────


def test_parse_slide_selection_reads_numbers_ranges_ids_and_all():
    slides = _deck()["slides"]
    assert parse_slide_selection("2-3, 5", slides) == [1, 2, 4]
    assert parse_slide_selection("4-", slides) == [3, 4]
    assert parse_slide_selection(["1", "3"], slides) == [0, 2]
    assert parse_slide_selection(2, slides) == [1]
    assert parse_slide_selection(slides[1]["id"], slides) == [1]
    assert parse_slide_selection("all", slides) == [0, 1, 2, 3, 4]
    assert parse_slide_selection("3,3,1", slides) == [2, 0]
    assert parse_slide_selection(None, slides) == []
    assert parse_slide_selection("", slides) == []
    with pytest.raises(PresentationPatchError, match="out of bounds"):
        parse_slide_selection("4-9", slides)
    with pytest.raises(PresentationPatchError, match="does not exist"):
        parse_slide_selection("7", slides)


def test_every_operation_has_a_contract_and_is_described():
    assert set(OPERATION_CONTRACTS) == set(OPERATIONS)
    text = describe_operations()
    for name in OPERATIONS:
        assert f"- {name} " in text, name
    assert set(pp._HANDLERS) == set(OPERATIONS)


# ── The tool ─────────────────────────────────────────────────────────────────


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
        self.statements: list = []
        self.added: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, *a, **k):
        self.statements.append(statement)
        return self._results.pop(0) if self._results else _Result()

    async def commit(self):
        self.committed = True

    async def flush(self):
        return None

    def add(self, obj):
        self.added.append(obj)


def _deck_lookup(existing, *, group_ids: list[str] | None = None) -> list[_Result]:
    return [_Result(rows=list(group_ids or [])), _Result(one=existing)]


def _existing_row(user_id: uuid.UUID, spec: dict | None = None, **columns):
    from app.models.presentation import Presentation

    spec = spec or _deck()
    return Presentation(
        id=uuid.uuid4(),
        user_id=user_id,
        title=spec["title"],
        theme=spec.get("theme") or "dark",
        slide_count=len(spec["slides"]),
        revision=1,
        spec=spec,
        **columns,
    )


def _build_tools(user_id: str):
    from app.services.builtin_tools import build_presentation_tools

    return build_presentation_tools(db=None, user_id=user_id, conversation_id=None)  # type: ignore[arg-type]


def _run_tool(executor, name: str, arguments: dict):
    from app.tools.base import ToolCall

    return asyncio.run(executor.execute_tool(ToolCall(id="call-1", name=name, arguments=arguments)))


def _patch_session(monkeypatch, session: _FakeSession) -> None:
    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: session)


def test_edit_presentation_changes_one_slide_and_proves_the_rest_untouched(monkeypatch):
    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    before = copy.deepcopy(existing.spec)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)

    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "replace_text", "slide": 3, "find": "12 MEUR", "replace": "12.4 MEUR"}],
        },
    )
    assert not result.is_error, result.content
    # The deck row is read FOR UPDATE: a concurrent editor of the same deck
    # waits and then patches the revision this call saved, never a stale one.
    from sqlalchemy.dialects import postgresql

    deck_select = str(session.statements[1].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE OF presentations" in deck_select
    # Same row, one revision up, one history snapshot with a derived summary.
    assert [type(r).__name__ for r in session.added] == ["PresentationVersion"]
    assert existing.revision == 2
    assert session.added[0].revision == 2
    assert session.added[0].summary == "Edited slide 3; 1 text replacement"
    assert session.committed
    # The word changed...
    assert existing.spec["slides"][2]["blocks"][0]["content"] == "ARR grew 40% to 12.4 MEUR."
    # ...and every other slide is byte-identical to what was stored.
    for i, slide in enumerate(before["slides"]):
        if i != 2:
            assert canonical(slide) == canonical(existing.spec["slides"][i]), i
    # The result says so, names the slide, and carries the marker + the ids.
    assert "1 slide changed, 4 unchanged - verified byte-identical to revision 1" in result.content
    assert "Changed:\n  3. [content] Numbers" in result.content
    # The marker points the chat at the slide the edit wrote.
    written_id = existing.spec["slides"][2]["id"]
    assert f"[[PRESENTATION:{existing.id}#/{written_id}]]" in result.content
    assert f"id={existing.spec['slides'][0]['id']}" in result.content
    assert "edit_presentation" in result.content


def test_edit_presentation_sets_one_dashboard_chart_in_place(monkeypatch):
    """The call that failed on 19 Oct 2026, end to end: update_block
    on a dashboard's panel saves one revision, the other panels and slides
    stay as stored, and the tool description teaches the address."""
    user_id = uuid.uuid4()
    existing = _existing_row(user_id, _dashboard_deck())
    before = copy.deepcopy(existing.spec)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)

    definitions, executor = _build_tools(str(user_id))
    edit = next(d for d in definitions if d.name == "edit_presentation")
    assert "on a DASHBOARD '2' = panel 2" in edit.description
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "expected_revision": "1",
            "operations": [{"op": "update_block", "slide": "2", "block": "1", "fields": {"value_label_fit": "stagger"}}],
        },
    )
    assert not result.is_error, result.content
    assert existing.revision == 2
    panels, stored = existing.spec["slides"][1]["panels"], before["slides"][1]["panels"]
    assert panels[0]["block"]["value_label_fit"] == "stagger"
    assert panels[0]["title"] == stored[0]["title"]
    assert [canonical(p) for p in panels[1:]] == [canonical(p) for p in stored[1:]]
    assert canonical(existing.spec["slides"][0]) == canonical(before["slides"][0])
    assert "1 slide changed, 1 unchanged" in result.content


def _template_row(template_id: uuid.UUID, **overrides):
    """A ``TemplateContainer`` row as ``load_visible_template_facts`` reads it."""
    from types import SimpleNamespace

    row = {
        "id": template_id,
        "name": "XTM One Brand Deck",
        "kind": "presentation",
        "theme_config": {"background": "hsl(220 54% 8%)", "accent": "#42caff"},
        "extra_metadata": {},
    }
    row.update(overrides)
    return SimpleNamespace(**row)


def _template_lookup(row) -> list[_Result]:
    """The two results a visibility-aware template load consumes: the caller's
    group memberships, then the template row as that caller may see it (``None``
    when it does not exist OR is not visible to them)."""
    return [_Result(rows=[]), _Result(one=row)]


def test_edit_presentation_applies_a_template_alone_and_snaps_the_theme(monkeypatch):
    """``template_id`` travels alone: no operation, no slide rewritten,
    the deck restyled - a template change IS an edit. The template decides the
    schemes the deck may be read in, so a light deck linked to a dark-only
    template is snapped to dark, the snap is STATED, and the ``Template:`` line
    names what the deck now wears and which themes it may pick."""
    user_id = uuid.uuid4()
    spec = _deck()
    spec["theme"] = "light"
    existing = _existing_row(user_id, spec)
    before = copy.deepcopy(existing.spec)
    template_id = uuid.uuid4()
    session = _FakeSession(results=[*_deck_lookup(existing), *_template_lookup(_template_row(template_id))])
    _patch_session(monkeypatch, session)

    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {"presentation_id": str(existing.id), "template_id": str(template_id)},
    )
    assert not result.is_error, result.content
    assert existing.template_container_id == template_id
    assert existing.revision == 2
    assert existing.spec["theme"] == "dark"
    assert existing.theme == "dark"
    assert session.committed
    # Every slide is byte-identical: the template is resolved on read, never
    # written into the slides.
    for i, slide in enumerate(before["slides"]):
        assert canonical(slide) == canonical(existing.spec["slides"][i]), i
    # The history line and the result account for the template, not for slides.
    assert [type(r).__name__ for r in session.added] == ["PresentationVersion"]
    assert session.added[0].summary.startswith("Applied template 'XTM One Brand Deck'")
    assert "Template: XTM One Brand Deck (themes: dark only)" in result.content
    assert "Theme snapped from 'light' to 'dark'" in result.content
    assert "supports dark only" in result.content
    assert f"[[PRESENTATION:{existing.id}]]" in result.content


def test_edit_presentation_insert_slide_takes_the_templates_cap(monkeypatch):
    """``insert_slide`` succeeds on a deck at the
    platform default whose template sets a higher ``max_slides`` (the facts
    are read BEFORE the operations run, as this user may see the template);
    the same insert on a free deck of the same length is refused, nothing
    written, and the refusal names the cap and how it is raised."""
    user_id = uuid.uuid4()
    template_id = uuid.uuid4()
    full = _deck(extra_slides=DEFAULT_SLIDE_CAP - 5)
    insert = {
        "op": "insert_slide",
        "after": 22,
        "content": {"layout": "section", "title": "Threat landscape, continued"},
    }
    existing = _existing_row(user_id, copy.deepcopy(full), template_container_id=template_id)
    board = _template_row(template_id, name="Board Deck", extra_metadata={"max_slides": 150})
    session = _FakeSession(results=[*_deck_lookup(existing), *_template_lookup(board)])
    _patch_session(monkeypatch, session)

    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(executor, "edit_presentation", {"presentation_id": str(existing.id), "operations": [insert]})
    assert not result.is_error, result.content
    assert existing.revision == 2
    assert len(existing.spec["slides"]) == DEFAULT_SLIDE_CAP + 1
    assert existing.spec["slides"][22]["title"] == "Threat landscape, continued"
    assert session.committed
    assert f"({DEFAULT_SLIDE_CAP + 1} slides, revision 2)" in result.content
    assert f"{DEFAULT_SLIDE_CAP} unchanged - verified byte-identical" in result.content

    # A free deck of the same length is held to the platform default.
    free = _existing_row(user_id, copy.deepcopy(full))
    session = _FakeSession(results=_deck_lookup(free))
    _patch_session(monkeypatch, session)
    result = _run_tool(executor, "edit_presentation", {"presentation_id": str(free.id), "operations": [insert]})
    assert result.is_error, result.content
    assert result.content.startswith("Nothing was changed: operation 1 (insert_slide): ")
    assert f"already has {DEFAULT_SLIDE_CAP} slides, its cap of {DEFAULT_SLIDE_CAP}" in result.content
    assert "max_slides" in result.content
    assert free.revision == 1
    assert len(free.spec["slides"]) == DEFAULT_SLIDE_CAP
    assert session.added == []
    assert not session.committed

    # The same deck under a template this user cannot see reads as free: the
    # cap is the default, and nothing about the template is echoed.
    hidden = _existing_row(user_id, copy.deepcopy(full), template_container_id=uuid.uuid4())
    session = _FakeSession(results=[*_deck_lookup(hidden), *_template_lookup(None)])
    _patch_session(monkeypatch, session)
    result = _run_tool(executor, "edit_presentation", {"presentation_id": str(hidden.id), "operations": [insert]})
    assert result.is_error, result.content
    assert f"its cap of {DEFAULT_SLIDE_CAP}" in result.content
    assert "Board Deck" not in result.content
    assert hidden.revision == 1


def test_edit_presentation_refuses_a_theme_the_linked_template_cannot_dress(monkeypatch):
    """An ``update_deck`` that SETS a theme the deck's template cannot dress is
    refused with the one shared sentence, never degraded: the user asked for a
    look that is not coming and must be told why. Nothing is written."""
    user_id = uuid.uuid4()
    template_id = uuid.uuid4()
    existing = _existing_row(user_id, template_container_id=template_id)
    session = _FakeSession(results=[*_deck_lookup(existing), *_template_lookup(_template_row(template_id))])
    _patch_session(monkeypatch, session)

    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "update_deck", "fields": {"theme": "light"}}],
        },
    )
    assert result.is_error, result.content
    assert result.content == (
        "Nothing was changed: Theme 'light' is not supported by the template "
        "'XTM One Brand Deck' (dark only). Pick a supported theme, link a template "
        "that supports 'light', or unlink the template."
    )
    assert existing.revision == 1
    assert existing.spec["theme"] == "dark"
    assert session.added == []
    assert not session.committed


def test_edit_presentation_answers_a_probe_that_finds_nothing_with_one_no_change_line(monkeypatch):
    """A ``replace_text`` whose text occurs nowhere is a no-op, not a failure:
    the tool answers in one line, ``is_error`` stays False (so four
    probes never trip the loop's failure brake), nothing is saved, and the
    result tells the model why so it can probe differently."""
    user_id = uuid.uuid4()
    _defs, executor = _build_tools(str(user_id))
    existing = _existing_row(user_id)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)

    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "replace_text", "find": "CERT-FR", "replace": "ANSSI"}],
        },
    )
    assert not result.is_error, result.content
    assert "\n" not in result.content, "one line"
    assert result.content == (
        "No change: operation 1 (replace_text): 'CERT-FR' was not found anywhere in the "
        "presentation (matching is exact and case-sensitive; search_presentation_text finds it in "
        "any case and shows the exact text); nothing was saved (revision "
        f"{existing.revision} kept). [[PRESENTATION:{existing.id}]]"
    )
    assert existing.revision == 1
    assert session.added == []
    assert not session.committed

    # The wording-already-there case is one line too.
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    title = existing.spec["slides"][0]["title"]
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "update_slide", "slide": 1, "fields": {"title": title}}],
        },
    )
    assert not result.is_error, result.content
    assert "\n" not in result.content
    assert result.content.startswith("No change: the presentation already reads that way")


def test_edit_presentation_unlinks_with_none_and_refuses_a_bad_template_id(monkeypatch):
    """``template_id: 'none'`` frees the deck (its content and theme stay);
    re-linking the template the deck already wears is not a change; a
    ``template_id`` that is neither a UUID nor a none-sentinel is refused with
    what the parameter accepts, and a UUID nobody can find as well."""
    user_id = uuid.uuid4()
    template_id = uuid.uuid4()
    _defs, executor = _build_tools(str(user_id))

    # 'none' unlinks: revision bumps, slides untouched, the line says free deck.
    existing = _existing_row(user_id, template_container_id=template_id)
    before = copy.deepcopy(existing.spec)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    result = _run_tool(
        executor, "edit_presentation", {"presentation_id": str(existing.id), "template_id": "none"}
    )
    assert not result.is_error, result.content
    assert existing.template_container_id is None
    assert existing.revision == 2
    assert canonical(before["slides"]) == canonical(existing.spec["slides"])
    assert session.added[0].summary == "Unlinked the template (free deck)"
    assert "Template: none (free deck" in result.content

    # Re-linking the template the deck already wears: no change, nothing saved.
    existing = _existing_row(user_id, template_container_id=template_id)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    result = _run_tool(
        executor, "edit_presentation", {"presentation_id": str(existing.id), "template_id": str(template_id)}
    )
    assert not result.is_error, result.content
    assert result.content.startswith("No change:")
    assert existing.revision == 1
    assert session.added == []

    # Not a UUID, not a sentinel: refused before anything is loaded.
    session = _FakeSession()
    _patch_session(monkeypatch, session)
    result = _run_tool(
        executor, "edit_presentation", {"presentation_id": str(existing.id), "template_id": "Brand Deck"}
    )
    assert result.is_error
    assert "template_id must be the UUID of a presentation template" in result.content
    assert session.statements == []

    # A UUID no template answers to - or one the user cannot see: the load is
    # visibility-checked, so both read as "not found"; refused after the
    # lookup, nothing written.
    existing = _existing_row(user_id)
    session = _FakeSession(results=[*_deck_lookup(existing), *_template_lookup(None)])
    _patch_session(monkeypatch, session)
    missing = uuid.uuid4()
    result = _run_tool(
        executor, "edit_presentation", {"presentation_id": str(existing.id), "template_id": str(missing)}
    )
    assert result.is_error
    assert f"no template `{missing}` was found" in result.content
    assert existing.revision == 1
    assert not session.committed

    # An asset library is not a deck template: refused by kind.
    library = uuid.uuid4()
    existing = _existing_row(user_id)
    session = _FakeSession(
        results=[*_deck_lookup(existing), *_template_lookup(_template_row(library, name="Partner logos", kind="asset-library"))]
    )
    _patch_session(monkeypatch, session)
    result = _run_tool(
        executor, "edit_presentation", {"presentation_id": str(existing.id), "template_id": str(library)}
    )
    assert result.is_error
    assert "'Partner logos' is not a presentation template (kind: asset-library)" in result.content
    assert existing.template_container_id is None
    assert not session.committed


def test_edit_presentation_links_the_slides_it_wrote(monkeypatch):
    """The account hands the agent the ADDRESS of every slide it changed -
    ``/presentations/<deck>#/<slide id>``, anchored on the stable id
    so the link survives a later insert or reorder - and tells it, once, to put
    the link in its reply. "Change applied, see slide 4" needs the link where
    the slide is named, not a form to assemble from an id further down."""
    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [
                {"op": "replace_text", "slide": 3, "find": "12 MEUR", "replace": "12.4 MEUR"},
                {"op": "insert_slide", "after": 3, "content": {"layout": "content", "title": "Risks", "blocks": [{"type": "text", "content": "None."}]}},
            ],
        },
    )
    assert not result.is_error, result.content
    slides = existing.spec["slides"]
    numbers_id, risks_id = slides[2]["id"], slides[3]["id"]
    assert slides[3]["title"] == "Risks"
    deck = str(existing.id)
    assert f"  3. [content] Numbers  link: /presentations/{deck}#/{numbers_id}" in result.content
    assert f"  4. [content] Risks  link: /presentations/{deck}#/{risks_id}" in result.content
    # One instruction, on the first slide written, as a ready-made Markdown link.
    assert f"Point the user at the change: put the slide's link in your reply, e.g. [slide 3](/presentations/{deck}#/{numbers_id})" in result.content
    # The marker carries the same focus, and only one marker is emitted.
    assert result.content.count("[[PRESENTATION:") == 1
    assert result.content.rstrip().endswith(f"[[PRESENTATION:{deck}#/{numbers_id}]]")
    # A slide id is URL-fragment safe by construction, so the link needs no
    # escaping: the ids the validator mints and the grammar agree on that.
    from app.services.presentation_spec import SLIDE_ID_PATTERN

    assert re.fullmatch(SLIDE_ID_PATTERN, numbers_id)


def test_edit_presentation_without_a_written_slide_keeps_the_plain_marker(monkeypatch):
    """A deck-level change (the title) rewrote no slide the user could be sent
    to: the marker stays the plain deck marker, no link line is emitted, and
    the ``presentation_saved`` frame carries no focus. A no-op keeps the plain
    marker as well."""
    from app.tools.tool_events import (
        PRESENTATION_SAVED_EVENT,
        bind_tool_event_sink,
        reset_tool_event_sink,
    )

    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    reported: list[dict] = []

    async def _sink(payload: dict) -> None:
        reported.append(payload)

    token = bind_tool_event_sink(_sink)
    try:
        _defs, executor = _build_tools(str(user_id))
        result = _run_tool(
            executor,
            "edit_presentation",
            {
                "presentation_id": str(existing.id),
                "operations": [{"op": "update_deck", "fields": {"title": "Board deck"}}],
            },
        )
    finally:
        reset_tool_event_sink(token)
    assert not result.is_error, result.content
    assert f"[[PRESENTATION:{existing.id}]]" in result.content
    assert "link: /presentations/" not in result.content
    assert "Point the user at the change" not in result.content
    assert reported == [
        {"event": PRESENTATION_SAVED_EVENT, "presentation_id": str(existing.id), "revision": 2}
    ]

    # No-op: nothing written, nothing to point at.
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "update_deck", "fields": {"title": "Board deck"}}],
        },
    )
    assert not result.is_error and "No change" in result.content
    assert f"[[PRESENTATION:{existing.id}]]" in result.content
    assert "#/" not in result.content


def test_edit_presentation_moved_slide_is_the_focus_when_nothing_was_written(monkeypatch):
    """A pure reorder writes no slide, but the moved slide IS what the user
    asked about: it gets the link and the focus."""
    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "move_slide", "slide": 3, "to": 2}],
        },
    )
    assert not result.is_error, result.content
    moved_id = existing.spec["slides"][1]["id"]
    assert existing.spec["slides"][1]["title"] == "Numbers"
    assert f"slide 3 -> 2 [content] Numbers  link: /presentations/{existing.id}#/{moved_id}" in result.content
    assert f"[[PRESENTATION:{existing.id}#/{moved_id}]]" in result.content


def test_edit_presentation_uses_the_agents_change_summary_when_given(monkeypatch):
    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "update_slide", "slide": 1, "fields": {"subtitle": "Board edition"}}],
            "change_summary": "Subtitle for the board",
        },
    )
    assert not result.is_error, result.content
    assert session.added[0].summary == "Subtitle for the board"
    assert 'History: "Subtitle for the board"' in result.content
    assert session.added[0].author_id == user_id


def test_edit_presentation_validation_error_writes_nothing(monkeypatch):
    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "update_block", "slide": 3, "block": "1", "fields": {"bogus": True}}],
        },
    )
    assert result.is_error
    # Field-addressed, verbatim, and nothing was saved.
    assert "slides.2" in result.content
    assert existing.revision == 1
    assert session.added == []
    assert not session.committed


def test_edit_presentation_patch_error_writes_nothing(monkeypatch):
    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {"presentation_id": str(existing.id), "operations": [{"op": "delete_slide", "slide": 99}]},
    )
    assert result.is_error
    assert "Nothing was changed" in result.content
    assert "slide 99 does not exist" in result.content
    assert existing.revision == 1 and session.added == []


def test_edit_presentation_no_change_is_not_a_revision(monkeypatch):
    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "update_slide", "slide": 5, "fields": {"title": "Thanks"}}],
        },
    )
    assert not result.is_error
    assert "No change" in result.content and "revision 1 kept" in result.content
    assert existing.revision == 1 and session.added == []


def test_edit_presentation_refuses_a_read_only_grantee(monkeypatch):
    owner, reader = uuid.uuid4(), uuid.uuid4()
    existing = _existing_row(
        owner,
        shared_user_ids=[str(reader)],
        shared_user_permissions={str(reader): "read_only"},
    )
    session = _FakeSession(results=_deck_lookup(existing) + _deck_lookup(existing))
    _patch_session(monkeypatch, session)
    _defs, executor = _build_tools(str(reader))
    result = _run_tool(
        executor,
        "edit_presentation",
        {"presentation_id": str(existing.id), "operations": [{"op": "delete_slide", "slide": 1}]},
    )
    assert result.is_error
    assert "read-only" in result.content
    assert "duplicate_presentation" in result.content
    assert existing.revision == 1 and session.added == []


def test_edit_presentation_refuses_a_deck_that_is_not_visible(monkeypatch):
    session = _FakeSession(results=_deck_lookup(None))
    _patch_session(monkeypatch, session)
    _defs, executor = _build_tools(str(uuid.uuid4()))
    result = _run_tool(
        executor,
        "edit_presentation",
        {"presentation_id": str(uuid.uuid4()), "operations": [{"op": "delete_slide", "slide": 1}]},
    )
    assert result.is_error and "was found" in result.content
    assert session.added == []
    bad = _run_tool(executor, "edit_presentation", {"presentation_id": "nope", "operations": []})
    assert bad.is_error


def test_edit_presentation_checks_file_references_on_the_written_slides_only(monkeypatch):
    """A colleague editing slide 2 of a shared deck must not be refused over the
    owner's own upload on slide 1 — the untouched slides keep the references
    they already had, the written ones are checked against the acting user."""
    from app.services import presentations as presentations_module

    owner, editor = uuid.uuid4(), uuid.uuid4()
    image_id = str(uuid.uuid4())
    spec = validate_spec(
        {
            "title": "Deck",
            "slides": [
                {"layout": "content", "title": "Diagram", "blocks": [{"type": "image", "file_id": image_id, "alt": "x"}]},
                {"layout": "content", "title": "Words", "blocks": [{"type": "text", "content": "Old words."}]},
            ],
        }
    )
    existing = _existing_row(
        owner, spec, shared_user_ids=[str(editor)], shared_user_permissions={str(editor): "config"}
    )
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    seen: list[dict] = []

    async def _capture(db, spec, *, user, **_kw):
        seen.append(copy.deepcopy(spec))
        return []

    monkeypatch.setattr(presentations_module, "verify_file_references", _capture)
    _defs, executor = _build_tools(str(editor))
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "replace_text", "slide": 2, "find": "Old", "replace": "New"}],
        },
    )
    assert not result.is_error, result.content
    assert len(seen) == 1
    checked = seen[0]["slides"]
    assert "blocks" not in checked[0] and checked[0]["id"] == spec["slides"][0]["id"]  # masked
    assert checked[1]["blocks"][0]["content"] == "New words."  # written, checked
    assert existing.user_id == owner and existing.updated_by_user_id == editor
    # The stored deck still carries the owner's image, untouched.
    assert existing.spec["slides"][0]["blocks"][0]["file_id"] == image_id


def _finding(index: int, slide_id: str | None, message: str):
    from app.services.presentation_layout import LayoutFinding

    return LayoutFinding(
        slide_index=index,
        slide_id=slide_id,
        layout="content",
        title=f"Slide {index + 1}",
        kind="fit",
        severity="overloaded",
        message=message,
        scale=0.7,
        amount=40,
    )


def test_edit_presentation_reports_the_layout_of_the_slides_it_wrote_only(monkeypatch):
    """The post-save layout check rides the edit result, scoped to the
    slides the edit wrote: a finding on an untouched slide is counted, never
    listed - listing it would send the agent off to "fix" a slide the user
    did not ask about, the deck-wide drift the tool exists to prevent."""
    from app.services import presentation_layout as layout
    from app.services.presentation_layout import LayoutReport

    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    ids = _ids(existing.spec)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    checked: list[tuple] = []

    async def _fake_check(presentation_id, user_uuid, spec, *, expected_revision=None):
        checked.append((presentation_id, user_uuid, expected_revision))
        return LayoutReport(
            slide_count=5,
            findings=(
                _finding(0, ids[0], "the title slide was already too dense"),
                _finding(2, ids[2], "the body needed 40% more height"),
                _finding(4, None, "an older finding published without an id"),
            ),
        )

    monkeypatch.setattr(layout, "check_presentation_layout", _fake_check)

    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "replace_text", "slide": 3, "find": "12 MEUR", "replace": "12.4 MEUR"}],
        },
    )
    assert not result.is_error, result.content
    # Checked once, on the saved deck, pinned to the revision the save produced.
    assert checked == [(existing.id, user_id, 2)]
    # Only the written slide's finding is listed, the others are counted.
    assert 'slide 3 [content] "Slide 3" (overloaded): the body needed 40% more height' in result.content
    assert "too dense" not in result.content and "without an id" not in result.content
    assert "Not listed: 2 pre-existing finding(s) on slides this edit did not touch" in result.content
    assert result.content.index("Layout check:") < result.content.index("presentation_id:")


def test_edit_presentation_reports_the_save_before_the_layout_render(monkeypatch):
    """A one-word edit reaches the stage when it is COMMITTED, not once
    the headless layout render has finished: the ``presentation_saved`` progress
    event fires after the commit and before the check, with the deck's id and
    the revision the save produced, so the studio / viewer / chat card refresh
    while the tool is still measuring."""
    from app.services import presentation_layout as layout
    from app.services.presentation_layout import LayoutReport
    from app.tools.tool_events import (
        PRESENTATION_SAVED_EVENT,
        bind_tool_event_sink,
        reset_tool_event_sink,
    )

    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    checked: list[tuple] = []
    reported: list[dict] = []

    async def _fake_check(presentation_id, user_uuid, spec, *, expected_revision=None):
        checked.append((presentation_id, expected_revision))
        return LayoutReport(slide_count=5)

    async def _sink(payload: dict) -> None:
        reported.append({**payload, "committed": session.committed, "checks_so_far": len(checked)})

    monkeypatch.setattr(layout, "check_presentation_layout", _fake_check)
    token = bind_tool_event_sink(_sink)
    try:
        _defs, executor = _build_tools(str(user_id))
        result = _run_tool(
            executor,
            "edit_presentation",
            {
                "presentation_id": str(existing.id),
                "operations": [{"op": "replace_text", "slide": 3, "find": "12 MEUR", "replace": "12.4 MEUR"}],
            },
        )
    finally:
        reset_tool_event_sink(token)

    assert not result.is_error, result.content
    assert reported == [
        {
            "event": PRESENTATION_SAVED_EVENT,
            "presentation_id": str(existing.id),
            "revision": 2,
            # The slide the edit wrote rides along: the studio stage
            # moves to it as the save lands.
            "focus_slide_id": existing.spec["slides"][2]["id"],
            "committed": True,
            "checks_so_far": 0,
        }
    ]
    assert checked == [(existing.id, 2)]


def test_edit_presentation_that_saves_nothing_reports_nothing(monkeypatch):
    """A refused edit never announces a save: the stage has nothing to fetch."""
    from app.tools.tool_events import bind_tool_event_sink, reset_tool_event_sink

    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    reported: list[dict] = []

    async def _sink(payload: dict) -> None:
        reported.append(payload)

    token = bind_tool_event_sink(_sink)
    try:
        _defs, executor = _build_tools(str(user_id))
        result = _run_tool(
            executor,
            "edit_presentation",
            {"presentation_id": str(existing.id), "operations": [{"op": "delete_slide", "slide": 99}]},
        )
    finally:
        reset_tool_event_sink(token)

    assert result.is_error
    assert not session.committed
    assert reported == []


def test_edit_presentation_layout_check_says_when_the_written_slide_fits(monkeypatch):
    from app.services import presentation_layout as layout
    from app.services.presentation_layout import LayoutReport

    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)

    async def _clean(*_a, **_k):
        return LayoutReport(slide_count=5)

    monkeypatch.setattr(layout, "check_presentation_layout", _clean)
    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "update_slide", "slide": 1, "fields": {"subtitle": "Board edition"}}],
        },
    )
    assert not result.is_error, result.content
    assert "Layout check: the slide this edit wrote fits its canvas." in result.content
    assert "Not listed" not in result.content


def test_edit_presentation_no_op_never_measures(monkeypatch):
    """Nothing was saved, so there is no new revision to render."""
    from app.services import presentation_layout as layout

    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)

    async def _never(*_a, **_k):
        raise AssertionError("a no-op must not render the deck")

    monkeypatch.setattr(layout, "check_presentation_layout", _never)
    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "update_slide", "slide": 1, "fields": {"title": existing.spec["slides"][0]["title"]}}],
        },
    )
    assert not result.is_error, result.content
    assert "No change" in result.content and "Layout check" not in result.content


def test_edit_presentation_deck_level_change_gets_the_whole_layout_report(monkeypatch):
    """A theme / ratio / chrome change re-renders EVERY slide, so narrowing the
    report to the (empty) set of written slides would hide what the edit did
    to all of them; the full report is the edit's consequence here."""
    from app.services import presentation_layout as layout
    from app.services.presentation_layout import LayoutReport

    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    ids = _ids(existing.spec)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)

    async def _fake_check(*_a, **_k):
        return LayoutReport(
            slide_count=5,
            findings=(_finding(2, ids[2], "the body needed 40% more height"),),
        )

    monkeypatch.setattr(layout, "check_presentation_layout", _fake_check)
    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "update_deck", "fields": {"theme": "light"}}],
        },
    )
    assert not result.is_error, result.content
    assert "Deck fields changed: theme" in result.content
    assert "the body needed 40% more height" in result.content
    assert "Not listed" not in result.content and "this edit wrote" not in result.content


def test_edit_presentation_structural_only_edit_measures_nothing(monkeypatch):
    """A move rewrites no slide's content: no rendering can have changed, so
    nothing is rendered - and the result never says "the 0 slides ... fit"."""
    from app.services import presentation_layout as layout

    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)

    async def _never(*_a, **_k):
        raise AssertionError("a move-only edit must not render the deck")

    monkeypatch.setattr(layout, "check_presentation_layout", _never)
    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "move_slide", "slide": 4, "to": 3}, {"op": "delete_slide", "slide": 5}],
        },
    )
    assert not result.is_error, result.content
    assert "Moved: slide 4 -> 3" in result.content and "Deleted: former slide 5" in result.content
    assert "Layout check" not in result.content and existing.revision == 2


def test_layout_deck_fields_are_the_deck_fields_drawn_on_a_slide():
    """The scoping constant is a subset of what ``update_deck`` may set and
    leaves out exactly the two fields no slide draws BY ITSELF: the deck title
    (the library / tab / chat-card name) and the transition (motion BETWEEN
    slides). The frontend chrome is the proof for the title: the
    component draws the text it is handed and reads no title at all; the ONE
    place the title reaches a slide is the template footer pattern
    (``chrome-pattern.ts``), and a deck wearing such a pattern widens
    the check per deck through ``template_chrome_redraws`` - never by adding
    the title to the global set."""
    assert LAYOUT_DECK_FIELDS.issubset(pp.DECK_FIELDS)
    assert set(pp.DECK_FIELDS) - LAYOUT_DECK_FIELDS == {"title", "transition"}
    chrome = _CHROME_TSX.read_text(encoding="utf-8")
    assert "text.footer" in chrome and "text.header" in chrome
    assert not re.search(r"\.title\b|\btitle\s*[:,}]", chrome)
    pattern = (_CHROME_TSX.parent / "chrome-pattern.ts").read_text(encoding="utf-8")
    assert "spec.title" in pattern and "event_label" in pattern and "footer_text" in pattern


@pytest.mark.parametrize(
    ("fields", "drawn_on_every_slide"),
    [
        ({"title": "Quarterly review, board edition"}, False),
        ({"transition": "fade"}, False),
        ({"theme": "light"}, True),
        ({"aspect_ratio": "4:3"}, True),
        ({"chrome": {"show_slide_numbers": False}}, True),
        ({"chrome": {"logo_position": "center"}}, True),
    ],
)
def test_edit_presentation_measures_the_whole_deck_only_for_a_field_drawn_on_every_slide(
    monkeypatch, fields, drawn_on_every_slide
):
    """A theme / ratio / chrome change redraws every slide, so the whole report
    is the edit's consequence; a rename or a transition change draws nothing
    on a slide, so alone it renders nothing (not "the 0 slides ... fit", and no
    headless browser for a rename) and, with a slide written in the same call,
    the report is narrowed to THAT slide as for any other edit."""
    from app.services import presentation_layout as layout
    from app.services.presentation_layout import LayoutReport

    user_id = uuid.uuid4()
    name = next(iter(fields))
    renders: list[int] = []
    existing = _existing_row(user_id)
    ids = _ids(existing.spec)

    async def _fake_check(*_a, **_k):
        renders.append(1)
        # A finding on slide 3 of the row being edited, which no call below writes.
        return LayoutReport(slide_count=5, findings=(_finding(2, ids[2], "the body needed 40% more height"),))

    monkeypatch.setattr(layout, "check_presentation_layout", _fake_check)
    _defs, executor = _build_tools(str(user_id))

    # The deck field alone.
    _patch_session(monkeypatch, _FakeSession(results=_deck_lookup(existing)))
    result = _run_tool(
        executor,
        "edit_presentation",
        {"presentation_id": str(existing.id), "operations": [{"op": "update_deck", "fields": fields}]},
    )
    assert not result.is_error, result.content
    assert f"Deck fields changed: {name}" in result.content and existing.revision == 2
    if drawn_on_every_slide:
        assert renders == [1] and "the body needed 40% more height" in result.content
        assert "Not listed" not in result.content
    else:
        assert renders == [] and "Layout check" not in result.content

    # The deck field plus a written slide (slide 1; the finding sits on slide 3).
    renders.clear()
    existing = _existing_row(user_id)
    ids = _ids(existing.spec)
    _patch_session(monkeypatch, _FakeSession(results=_deck_lookup(existing)))
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [
                {"op": "update_deck", "fields": fields},
                {"op": "update_slide", "slide": 1, "fields": {"subtitle": "Board edition"}},
            ],
        },
    )
    assert not result.is_error, result.content
    assert renders == [1]
    if drawn_on_every_slide:
        assert "the body needed 40% more height" in result.content and "Not listed" not in result.content
    else:
        assert "the body needed 40% more height" not in result.content
        assert "Not listed: 1 pre-existing finding(s) on slides this edit did not touch" in result.content


def test_edit_presentation_accounts_against_the_validated_spec(monkeypatch):
    """An operation the validator normalises away (a title with a trailing
    space, a deck title that strips back to itself) is "already as requested",
    not "changed": the result and the history name only what really differs."""
    from app.models.presentation import PresentationVersion

    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [
                {"op": "update_slide", "slide": 3, "fields": {"title": "Numbers "}},
                {"op": "update_deck", "fields": {"title": "Quarterly review "}},
                {"op": "update_slide", "slide": 5, "fields": {"title": "Thank you"}},
            ],
        },
    )
    assert not result.is_error, result.content
    assert "1 slide changed, 4 unchanged" in result.content
    assert "Changed:\n  5. [closing] Thank you" in result.content
    assert "Already as requested (named, unchanged): slide 3" in result.content
    assert "Deck fields changed" not in result.content
    versions = [r for r in session.added if isinstance(r, PresentationVersion)]
    assert [v.summary for v in versions] == ["Edited slide 5"]


def test_reconcile_recomputes_the_account_from_the_validated_spec():
    from app.services.presentation_patch import reconcile

    spec = _deck()
    out = apply_operations(
        spec,
        [
            {"op": "update_slide", "slide": 1, "fields": {"title": "Quarterly review "}},
            {"op": "update_slide", "slide": 3, "fields": {"title": "Figures"}},
            {"op": "update_deck", "fields": {"title": "Quarterly review "}},
            {"op": "move_slide", "slide": 4, "to": 2},
        ],
    )
    # Before validation the tracker saw slide 1 and the deck title change ...
    assert [r.index for r in out.changed] == [1, 4] and out.deck_fields == ["title"]
    # ... the validator strips both back, and only slide 3 (now 4) really changed.
    patched = validate_spec(out.spec)
    assert patched["slides"][0]["title"] == "Quarterly review" and patched["title"] == "Quarterly review"
    settled = reconcile(out, spec, patched)
    assert [r.index for r in settled.changed] == [4]
    assert [r.index for r in settled.named_unchanged] == [1]
    assert settled.deck_fields == [] and settled.spec == patched
    assert [(ref.index, old) for ref, old in settled.moved] == [(2, 4)]
    assert settled.touched_ids == {_ids(spec)[2]}
    assert verify_untouched(spec, patched, settled.touched_ids) == []
    assert summarize_patch(settled, patched) == "Edited slide 4; Moved slide 4 to 2"
    _assert_untouched_identical(spec, patched, settled.touched_ids)


def test_edit_presentation_refuses_a_stale_revision(monkeypatch):
    """A slide NUMBER read at revision 1 means nothing at revision 3 if a
    slide was inserted in between: with expected_revision the call is refused
    before any operation resolves, and the agent re-reads."""
    from app.models.presentation import PresentationVersion

    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    existing.revision = 3
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    _defs, executor = _build_tools(str(user_id))
    stale = {
        "presentation_id": str(existing.id),
        "operations": [{"op": "update_slide", "slide": 5, "fields": {"title": "Thank you"}}],
        "expected_revision": 1,
    }
    result = _run_tool(executor, "edit_presentation", stale)
    assert result.is_error
    assert "moved from revision 1 to 3" in result.content and "Nothing was changed" in result.content
    # The cause is a later save, not "someone else": the usual one is the
    # agent's own earlier edit, and the wording is relayed to the user.
    assert "an earlier edit of yours, or someone else's" in result.content
    assert "someone else saved" not in result.content
    assert not [r for r in session.added if isinstance(r, PresentationVersion)]
    assert existing.revision == 3 and existing.spec["slides"][4]["title"] == "Thanks"

    # The revision the agent read, as an int, as the digit string some
    # providers send for an integer field or as the float others send, lets
    # the edit through - the same lenience a slide number gets.
    for revision in (3, "3", 3.0):
        fresh = _FakeSession(results=_deck_lookup(existing))
        _patch_session(monkeypatch, fresh)
        result = _run_tool(executor, "edit_presentation", {**stale, "expected_revision": revision})
        assert not result.is_error, result.content
        existing.spec["slides"][4]["title"] = "Thanks"
        existing.revision = 3
    # Anything that is not a revision number is refused as such - a revision
    # starts at 1, so 0 is not a stale revision, it is not a revision.
    for value in ("latest", 0, 2.5):
        result = _run_tool(executor, "edit_presentation", {**stale, "expected_revision": value})
        assert result.is_error and "expected_revision must be the revision number" in result.content, value
        assert "moved from revision" not in result.content


def test_coerce_revision_reads_like_a_slide_number_from_one_up():
    """One coercion for a slide number and a revision: an int, an
    integer-valued float, a digit string; a revision starts at 1."""
    for value in (3, "3", " 3 ", 3.0):
        assert coerce_revision(value) == 3, value
    for value in (0, -1, 0.0, 2.5, True, False, None, "", "latest", "3.5", "-1"):
        assert coerce_revision(value) is None, value


def test_edit_presentation_schema_offers_expected_revision():
    defs, _executor = _build_tools(str(uuid.uuid4()))
    edit = next(d for d in defs if d.name == "edit_presentation")
    assert edit.input_schema["properties"]["expected_revision"]["type"] == "integer"
    assert "expected_revision" not in edit.input_schema["required"]
    assert "expected_revision" in edit.description


def test_narrow_layout_report_matches_by_id_then_by_position_and_keeps_a_skip():
    from app.services.presentation_layout import LayoutReport, narrow_layout_report

    report = LayoutReport(
        slide_count=5,
        findings=(
            _finding(0, "aaaaaaaaaaaa", "a"),
            _finding(2, "cccccccccccc", "c"),
            _finding(3, None, "d - published without an id"),
            _finding(4, "eeeeeeeeeeee", "e"),
        ),
        unit="page",
        revision=7,
    )
    narrowed, elsewhere = narrow_layout_report(report, slide_ids={"cccccccccccc", "dddddddddddd"}, slide_indexes={2, 3})
    assert [f.message for f in narrowed.findings] == ["c", "d - published without an id"]
    assert elsewhere == 2
    assert (narrowed.slide_count, narrowed.unit, narrowed.revision) == (2, "page", 7)
    # A finding whose id moved position is still found by its id.
    moved, _ = narrow_layout_report(report, slide_ids={"eeeeeeeeeeee"}, slide_indexes={1})
    assert [f.message for f in moved.findings] == ["e"]
    # A skipped report carries no usable findings and passes through untouched.
    skipped = LayoutReport(slide_count=5, skipped="the browser timed out", findings=report.findings)
    assert narrow_layout_report(skipped, slide_ids={"cccccccccccc"}, slide_indexes={2}) == (skipped, 0)


def test_edit_presentation_is_registered_between_generate_and_get():
    defs, executor = _build_tools(str(uuid.uuid4()))
    names = [d.name for d in defs]
    assert names[:3] == ["generate_presentation", "edit_presentation", "get_presentation"]
    assert "edit_presentation" in executor.tool_names
    edit = next(d for d in defs if d.name == "edit_presentation")
    assert edit.source == "builtin:generate_presentation"
    # The description carries the operation catalog, the schema its enum.
    for name in OPERATIONS:
        assert f"- {name} " in edit.description, name
    assert edit.input_schema["properties"]["operations"]["items"]["properties"]["op"]["enum"] == list(OPERATIONS)
    # A template change alone is a complete edit, so operations are no
    # longer required at the schema level - the handler refuses a call that
    # carries neither.
    assert edit.input_schema["required"] == ["presentation_id"]
    assert "template_id" in edit.input_schema["properties"]
    assert "search_templates" in edit.input_schema["properties"]["template_id"]["description"]
    # The wholesale path now routes partial changes here.
    generate = next(d for d in defs if d.name == "generate_presentation")
    assert "edit_presentation" in generate.input_schema["properties"]["presentation_id"]["description"]
    assert "edit_presentation" in generate.description
    get = next(d for d in defs if d.name == "get_presentation")
    assert "slides" in get.input_schema["properties"]
    assert "never cut" in get.description


def _schema_types(node: object):
    if isinstance(node, dict):
        if "type" in node:
            yield node["type"]
        for value in node.values():
            yield from _schema_types(value)
    elif isinstance(node, list):
        for value in node:
            yield from _schema_types(value)


def test_edit_schema_declares_one_type_per_property_so_gemini_accepts_it():
    """A `"type": ["integer", "string"]` union is valid JSON Schema and fine for
    Anthropic / OpenAI, but Gemini's function declarations refuse it — and one
    refused tool fails EVERY request of an agent that carries the deck tools.
    The schema therefore declares single types; the engine still reads a digit
    string as a position."""
    from app.services.builtin_tools import build_presentation_tools

    defs, _executor = build_presentation_tools(
        db=None,  # type: ignore[arg-type]
        user_id=str(uuid.uuid4()),
        conversation_id=None,
    )
    assert {d.name for d in defs} >= {
        "generate_presentation",
        "edit_presentation",
        "get_presentation",
        "export_presentation",
    }
    for definition in defs:
        for declared in _schema_types(definition.input_schema):
            assert isinstance(declared, str), (definition.name, declared)
    genai_types = pytest.importorskip("google.genai.types")
    from app.llm.gemini_provider import _clean_schema_for_gemini

    # The provider's own conversion (gemini_provider.py, the `tools` branch of
    # chat / stream): clean, declare, wrap in one Tool - any refusal raises here.
    declarations = []
    for definition in defs:
        schema = _clean_schema_for_gemini(definition.input_schema)
        declarations.append(
            genai_types.FunctionDeclaration(
                name=definition.name,
                description=definition.description,
                parameters=schema if schema.get("properties") else None,
            )
        )
    tool = genai_types.Tool(function_declarations=declarations)
    assert len(tool.function_declarations) == len(defs)
    # The string form is what the engine gets; it addresses like the integer did.
    deck = _deck()
    by_string = apply_operations(deck, [{"op": "update_slide", "slide": "5", "fields": {"title": "Bye"}}])
    by_int = apply_operations(deck, [{"op": "update_slide", "slide": 5, "fields": {"title": "Bye"}}])
    assert canonical(by_string.spec) == canonical(by_int.spec)
    inserted = apply_operations(
        deck,
        [{"op": "insert_slide", "at": "2", "content": {"layout": "section", "title": "Two"}}],
    )
    assert inserted.spec["slides"][1]["title"] == "Two"


def _stored_v1_deck() -> dict:
    """A deck stored in the v1 shape, as ``validate_spec`` now reads it: the v1
    ``generic`` slide is already a ``content`` slide whose words sit in a
    ``text`` block - there is no ``rich_text`` field left to write into."""
    spec = validate_spec(
        {
            "title": "Legacy deck",
            "slides": [
                {"layout": "title", "title": "Legacy", "subtitle": "v1"},
                {"layout": "generic", "title": "Legacy", "rich_text": "<p>Hello</p>"},
            ],
        }
    )
    assert spec["slides"][1]["layout"] == "content"
    assert spec["slides"][1]["blocks"][0] == {**spec["slides"][1]["blocks"][0], "type": "text", "content": "Hello"}
    return spec


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "update_slide", "slide": 2, "fields": {"rich_text": "<p>Hi</p><script>alert(1)</script>"}},
        {"op": "replace_text", "slide": 2, "find": "Hello", "replace": "<img src=x onerror=alert(1)>"},
    ],
)
def test_a_surgical_write_of_html_is_refused_and_nothing_is_written(monkeypatch, operation):
    """No slide field carries HTML: ``rich_text`` is not a field
    (an unknown key on a ``content`` slide), and a tag written into a text
    block is refused by the inline-markup grammar. A PATCH goes through the
    same validation as a fresh deck, so the surgical path is not a way around
    either, and nothing is written."""
    legacy = _stored_v1_deck()
    outcome = apply_operations(legacy, [operation])
    with pytest.raises(PresentationSpecError) as excinfo:
        validate_spec(outcome.spec)
    assert "slides.1" in str(excinfo.value)

    user_id = uuid.uuid4()
    existing = _existing_row(user_id, legacy)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(executor, "edit_presentation", {"presentation_id": str(existing.id), "operations": [operation]})
    assert result.is_error and "slides.1" in result.content
    assert existing.revision == 1 and session.added == [] and not session.committed
    assert existing.spec["slides"][1]["blocks"][0]["content"] == "Hello"


def test_a_v1_slide_written_by_an_operation_is_upgraded_and_carries_no_html(monkeypatch):
    """An agent that still swaps in a retired v1 slide gets the v2
    slide that says the same thing: the fragment's tags never reach storage
    (an embed has no words, so the slide becomes a titled section), the
    other slides stay byte-identical, and the result says what happened so
    the agent stops emitting the shape."""
    legacy = _stored_v1_deck()
    operation = {
        "op": "replace_slide",
        "slide": 2,
        "content": {"layout": "generic", "title": "Legacy", "rich_text": "<iframe src='x'></iframe>"},
    }

    user_id = uuid.uuid4()
    existing = _existing_row(user_id, legacy)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(executor, "edit_presentation", {"presentation_id": str(existing.id), "operations": [operation]})

    assert not result.is_error, result.content
    assert "retired v1 layout" in result.content and "generic" in result.content
    assert existing.revision == 2 and session.committed
    stored = json.dumps(existing.spec)
    assert "rich_text" not in stored and "<" not in stored and "iframe" not in stored
    assert existing.spec["slides"][1]["layout"] == "section"
    assert existing.spec["slides"][1]["title"] == "Legacy"
    assert existing.spec["slides"][0] == legacy["slides"][0]


# ── Read-back: whole slides, never cut ───────────────────────────────────────


def test_get_presentation_slides_selector_returns_the_named_slides_whole(monkeypatch):
    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    _defs, executor = _build_tools(str(user_id))
    content = _run_tool(
        executor, "get_presentation", {"presentation_id": str(existing.id), "slides": "3, 5"}
    ).content
    # The outline is always complete, with ids.
    for i, slide in enumerate(existing.spec["slides"]):
        assert f"{i + 1}. [{slide['layout']}]" in content
        assert f"id={slide['id']}" in content
    # Only the selected slides are dumped, whole and parseable.
    assert "--- slide 3 [content]" in content and "--- slide 5 [closing]" in content
    assert "--- slide 1 " not in content and "--- slide 4 " not in content
    body = content.split("Slides 3, 5 in full:\n", 1)[1]
    payloads = [line for line in body.splitlines() if line.startswith("{")]
    assert [json.loads(p)["title"] for p in payloads] == ["Numbers", "Thanks"]
    assert "Deck settings: {" in content
    assert "edit it in place" in content and "edit_presentation" in content
    assert f"[[PRESENTATION:{existing.id}]]" in content


def test_get_presentation_over_budget_is_served_whole_slide_by_whole_slide(monkeypatch):
    """The failure: 'the read tool truncates after slide 15'.
    A deck over the budget is never cut mid-JSON — it is paged by whole slides
    and the tool names the slides left and how to read them."""
    from app.services.builtin_tools import presentation as tool_module

    user_id = uuid.uuid4()
    existing = _existing_row(user_id, _deck(extra_slides=20))
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(tool_module, "_SPEC_READBACK_BUDGET", 3000)
    _defs, executor = _build_tools(str(user_id))
    content = _run_tool(executor, "get_presentation", {"presentation_id": str(existing.id)}).content

    assert "truncated" not in content
    assert "never cut mid-slide" in content
    shown = [line for line in content.splitlines() if line.startswith("{")]
    assert 1 <= len(shown) < 25
    for payload in shown:
        json.loads(payload)  # every dumped slide is complete JSON
    first_left = len(shown) + 1
    assert f"Not shown: slides {first_left}-25" in content
    assert f'slides="{first_left}-25"' in content
    # The outline still lists all 25 slides.
    assert "25. [content] Extra 20" in content

    # And the follow-up call reads the tail in full.
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(tool_module, "_SPEC_READBACK_BUDGET", 10_000_000)
    tail = _run_tool(
        executor, "get_presentation", {"presentation_id": str(existing.id), "slides": f"{first_left}-25"}
    ).content
    dumped = [json.loads(line) for line in tail.splitlines() if line.startswith("{")]
    assert [s["title"] for s in dumped][-1] == "Extra 20"
    assert len(dumped) == 25 - first_left + 1


def test_get_presentation_rejects_a_selector_that_names_nothing(monkeypatch):
    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)
    _defs, executor = _build_tools(str(user_id))
    content = _run_tool(
        executor, "get_presentation", {"presentation_id": str(existing.id), "slides": "40-45"}
    ).content
    assert content.startswith("Error:") and "out of bounds" in content


# ── Prompts route changes to the surgical path ───────────────────────────────


def test_the_studio_prompt_and_the_guidance_route_edits_to_edit_presentation():
    from types import SimpleNamespace

    from app.services.presentation_guidance import build_generate_presentation_description
    from app.services.presentation_studio import _ACCESS_NOTES, StudioContext, format_studio_prompt

    description = build_generate_presentation_description()
    assert "edit_presentation" in description
    assert "byte-identical" in description
    deck = SimpleNamespace(id=uuid.uuid4(), title="Deck", revision=3, spec=_deck())
    ctx = StudioContext(presentation=deck, permission="owner", focused_index=None)  # type: ignore[arg-type]
    prompt = format_studio_prompt(ctx)
    assert "edit_presentation" in prompt
    assert f"presentation_id={deck.id}" in prompt
    assert "edit_presentation" in _ACCESS_NOTES["read_only"]


# ── Per-slide themes ─────────────────────────────────────────────────


def _themed_deck() -> dict:
    """The baseline deck with a dark cover and a light closing on a light deck."""
    spec = _deck()
    spec["theme"] = "light"
    spec["slides"][0]["theme"] = "dark"
    spec["slides"][0]["hero"] = "gradient"
    spec["slides"][4]["theme"] = "light"
    return validate_spec(spec)


def test_update_deck_theme_leaves_overridden_slides_byte_identical():
    """A deck theme change reaches only the slides that inherit: the cover that
    names its own theme is not touched, not rewritten, not normalised - and
    the account says how far the change went."""
    from app.services.presentations import deck_theme_change_note

    baseline = _themed_deck()
    result = apply_operations(baseline, [{"op": "update_deck", "fields": {"theme": "dark"}}])
    patched = validate_spec(result.spec)
    assert patched["theme"] == "dark"
    for index, slide in enumerate(baseline["slides"]):
        assert canonical(slide) == canonical(patched["slides"][index]), index
    assert patched["slides"][0]["theme"] == "dark" and patched["slides"][4]["theme"] == "light"
    assert verify_untouched(baseline, patched, set()) == []
    note = deck_theme_change_note(patched)
    assert note.startswith("Deck theme 'dark': 3 slides inherited the change")
    assert "2 kept their own theme" in note and "slides 1 (dark), 5 (light)" in note
    # Back to light: still nothing moves on the overridden slides.
    back = validate_spec(apply_operations(patched, [{"op": "update_deck", "fields": {"theme": "light"}}]).spec)
    assert back["slides"][0]["theme"] == "dark" and back["slides"][4]["theme"] == "light"
    # A deck with no override says so in fewer words.
    assert deck_theme_change_note(_deck()) == "Deck theme 'dark': 5 slides inherited the change."
    # One override reads in the singular - the sentence is quoted to the user.
    single = _deck()
    single["slides"][0]["theme"] = "dark"
    assert deck_theme_change_note(validate_spec(single)) == (
        "Deck theme 'dark': 4 slides inherited the change; 1 kept its own theme and was left as it is: "
        "slide 1 (dark) (update_slide {theme: null} gives one back to the deck)."
    )


def test_update_slide_theme_null_gives_the_slide_back_to_the_deck():
    baseline = _themed_deck()
    result = apply_operations(baseline, [{"op": "update_slide", "slide": "1", "fields": {"theme": None}}])
    patched = validate_spec(result.spec)
    assert "theme" not in patched["slides"][0]
    assert patched["slides"][0]["hero"] == "gradient"  # the look stays, the scheme follows the deck
    assert [ref.index for ref in result.changed] == [1]
    assert verify_untouched(baseline, patched, result.touched_ids) == []
    # And the other way: a slide takes a theme of its own in one operation.
    themed = validate_spec(
        apply_operations(baseline, [{"op": "update_slide", "slide": "2", "fields": {"theme": "dark"}}]).spec
    )
    assert themed["slides"][1]["theme"] == "dark"


def test_replace_text_never_rewrites_a_hero_or_an_overlay():
    baseline = _themed_deck()
    baseline["slides"][0]["overlay"] = "soft"
    baseline["slides"][0]["hero_tone"] = "accent"
    baseline["slides"][0]["background_image_file_id"] = str(uuid.uuid4())
    baseline = validate_spec(baseline)
    with pytest.raises(PresentationPatchError):
        apply_operations(baseline, [{"op": "replace_text", "find": "gradient", "replace": "plain"}])
    with pytest.raises(PresentationPatchError):
        apply_operations(baseline, [{"op": "replace_text", "find": "soft", "replace": "strong"}])
    with pytest.raises(PresentationPatchError):
        apply_operations(baseline, [{"op": "replace_text", "find": "accent", "replace": "error"}])
    # The colour IS an edit through update_slide, like the hero itself.
    result = apply_operations(baseline, [{"op": "update_slide", "slide": "1", "fields": {"hero_tone": "gradient_from"}}])
    assert result.spec["slides"][0]["hero_tone"] == "gradient_from"
    assert result.spec["slides"][0]["hero"] == "gradient"


def test_slide_theme_helpers_hold_slides_to_the_template_like_the_deck():
    from app.services.presentations import (
        fit_slide_themes_to_schemes,
        slide_theme_snap_note,
        unsupported_slide_theme_message,
        unsupported_slide_themes,
    )

    spec = _themed_deck()
    assert unsupported_slide_themes(spec, ["dark", "light"]) == []
    assert unsupported_slide_themes(spec, ["dark"]) == [(4, "light")]
    assert unsupported_slide_themes(spec, ["light"]) == [(0, "dark")]
    assert unsupported_slide_themes(spec, []) == [(0, "dark"), (4, "light")]
    message = unsupported_slide_theme_message(
        [(4, "light")], spec=spec, template_name="XTM One Brand Deck", supported_schemes=["dark"]
    )
    assert message.startswith("Slide 5 theme 'light': not supported by the template 'XTM One Brand Deck' (dark only).")
    assert "remove its theme so it inherits the deck's" in message
    snapped = fit_slide_themes_to_schemes(spec, ["dark"])
    assert snapped == [(4, "light", "dark")]
    assert spec["slides"][4]["theme"] == "dark" and spec["slides"][0]["theme"] == "dark"
    assert "theme" not in spec["slides"][1]  # an inheriting slide is never written
    assert slide_theme_snap_note(snapped, spec=spec, template_name="XTM One Brand Deck") == (
        "1 slide kept a theme of its own that the template 'XTM One Brand Deck' cannot dress "
        "and was snapped (5: 'light' -> 'dark')."
    )
    # Two snapped slides read in the plural - the sentence is quoted to the user.
    assert slide_theme_snap_note(
        [(0, "light", "dark"), (4, "light", "dark")], spec=spec, template_name="XTM One Brand Deck"
    ) == (
        "2 slides kept a theme of their own that the template 'XTM One Brand Deck' cannot dress "
        "and were snapped (1: 'light' -> 'dark'; 5: 'light' -> 'dark')."
    )
    assert fit_slide_themes_to_schemes(spec, []) == []
    assert slide_theme_snap_note([], spec=spec, template_name="x") == ""


def test_update_slide_sets_a_dashboard_on_two_columns_and_the_validator_holds_its_spans():
    """`panel_columns` is a slide field like any other: one
    update_slide sets it, the other slides stay byte-identical, a span the two
    columns cannot hold is refused at validation, and null gives the slide
    back its three columns."""
    chart = {"type": "chart", "kind": "bar", "categories": ["Q1", "Q2"], "series": [{"name": "ARR", "values": [1, 2]}]}
    dashboard = {
        "layout": "dashboard",
        "title": "Adoption",
        "tiles": [{"value": "1", "label": "a"}, {"value": "2", "label": "b"}],
        "panels": [{"title": "Trend", "block": chart}, {"title": "Mix", "block": chart}],
    }
    deck = _deck()
    baseline = validate_spec({**deck, "slides": [*deck["slides"], dashboard]})
    assert baseline["slides"][-1]["panel_columns"] == 3

    result = apply_operations(baseline, [{"op": "update_slide", "slide": "6", "fields": {"panel_columns": 2}}])
    patched = validate_spec(result.spec)
    assert patched["slides"][-1]["panel_columns"] == 2
    assert [ref.index for ref in result.changed] == [6]
    assert verify_untouched(baseline, patched, result.touched_ids) == []

    full_width = {**baseline["slides"][-1], "panels": [{"span": 3, "block": chart}]}
    wide = validate_spec({**baseline, "slides": [*baseline["slides"][:-1], full_width]})
    refused = apply_operations(wide, [{"op": "update_slide", "slide": "6", "fields": {"panel_columns": 2}}])
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(refused.spec)
    assert "panels.0.span (3) is wider than the panel grid" in str(exc.value)

    restored = validate_spec(
        apply_operations(patched, [{"op": "update_slide", "slide": "6", "fields": {"panel_columns": None}}]).spec
    )
    assert restored["slides"][-1]["panel_columns"] == 3


def test_update_slide_leaves_an_absent_look_field_alone_and_a_null_one_falls_back():
    """The absent-vs-null contract of the patch tool: an operation that
    does not NAME `theme` (or `hero`, `hero_tone`, `overlay`, `hero_accent_word`)
    leaves the slide's own value where it is - only an explicit null removes
    it, and a removed look field reads as the model's default again."""
    baseline = _themed_deck()
    baseline["slides"][0]["hero_tone"] = "accent"
    baseline["slides"][0]["hero_accent_word"] = True
    baseline["slides"][0]["overlay"] = "soft"
    baseline["slides"][0]["background_image_file_id"] = str(uuid.uuid4())
    baseline = validate_spec(baseline)

    # A title edit that says nothing about the look changes nothing about it.
    retitled = validate_spec(
        apply_operations(baseline, [{"op": "update_slide", "slide": "1", "fields": {"title": "Renamed"}}]).spec
    )
    cover = retitled["slides"][0]
    assert cover["title"] == "Renamed"
    assert cover["theme"] == "dark" and cover["hero"] == "gradient" and cover["hero_tone"] == "accent"
    assert cover["hero_accent_word"] is True and cover["overlay"] == "soft"

    # Every look field nulled at once: the slide inherits the deck's scheme and
    # the defaults of the models come back - never an error, never a stale value.
    cleared = validate_spec(
        apply_operations(
            baseline,
            [
                {
                    "op": "update_slide",
                    "slide": "1",
                    "fields": {"theme": None, "hero": None, "hero_tone": None, "overlay": None, "hero_accent_word": None},
                }
            ],
        ).spec
    )
    cover = cleared["slides"][0]
    assert "theme" not in cover
    assert cover["hero"] == "plain" and cover["hero_tone"] == "brand"
    assert cover["overlay"] == "none" and cover["hero_accent_word"] is False
    assert cover["background_image_file_id"] == baseline["slides"][0]["background_image_file_id"]


def test_edit_presentation_refuses_a_slide_theme_the_linked_template_cannot_dress(monkeypatch):
    """Setting a slide's own theme under the deck's template follows the deck
    theme's rule: a scheme the template cannot dress is refused with the slide
    and the field, and nothing is written."""
    user_id = uuid.uuid4()
    template_id = uuid.uuid4()
    existing = _existing_row(user_id, template_container_id=template_id)
    session = _FakeSession(results=[*_deck_lookup(existing), *_template_lookup(_template_row(template_id))])
    _patch_session(monkeypatch, session)

    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "update_slide", "slide": "1", "fields": {"theme": "light"}}],
        },
    )
    assert result.is_error, result.content
    assert result.content.startswith(
        "Nothing was changed: Slide 1 theme 'light': not supported by the template "
        "'XTM One Brand Deck' (dark only)."
    )
    assert existing.revision == 1
    assert "theme" not in existing.spec["slides"][0]
    assert session.added == []
    assert not session.committed


def test_introduced_slide_themes_compares_by_slide_id_never_by_position():
    """The helper behind both write paths (REST PATCH and edit_presentation):
    a stored override survives a move, a replacement at the same position is
    a new theme, and a row older than slide ids is compared by position."""
    from app.services.presentations import introduced_slide_themes

    baseline = _themed_deck()  # slide 1 dark, slide 5 light, ids assigned
    cover_id, closing_id = baseline["slides"][0]["id"], baseline["slides"][4]["id"]
    offending = [(0, "dark"), (4, "light")]
    # The same deck: nothing introduced.
    assert introduced_slide_themes(baseline, baseline, offending) == []
    # The closing moved to the front and the cover after it: still nothing new.
    moved = copy.deepcopy(baseline)
    moved["slides"] = [moved["slides"][4], moved["slides"][0], *moved["slides"][1:4]]
    assert moved["slides"][0]["id"] == closing_id and moved["slides"][1]["id"] == cover_id
    assert introduced_slide_themes(baseline, moved, [(0, "light"), (1, "dark")]) == []
    # A NEW slide (no id) at the cover's old position, naming the cover's scheme.
    inserted = copy.deepcopy(baseline)
    inserted["slides"].insert(0, {"layout": "title", "title": "New", "theme": "dark"})
    assert introduced_slide_themes(baseline, inserted, [(0, "dark"), (1, "dark"), (5, "light")]) == [(0, "dark")]
    # The cover switched to the other scheme: a new theme on a known slide.
    switched = copy.deepcopy(baseline)
    switched["slides"][0]["theme"] = "light"
    assert introduced_slide_themes(baseline, switched, [(0, "light"), (4, "light")]) == [(0, "light")]
    # A row older than slide ids is compared by position, and only then.
    legacy = copy.deepcopy(baseline)
    for slide in legacy["slides"]:
        slide.pop("id", None)
    legacy_edit = copy.deepcopy(legacy)
    legacy_edit["slides"][0]["title"] = "Renamed"
    assert introduced_slide_themes(legacy, legacy_edit, offending) == []
    legacy_moved = copy.deepcopy(legacy)
    legacy_moved["slides"] = [legacy_moved["slides"][4], *legacy_moved["slides"][:4]]
    assert introduced_slide_themes(legacy, legacy_moved, [(0, "light"), (1, "dark")]) == [(0, "light"), (1, "dark")]


def test_edit_presentation_tolerates_a_stored_slide_theme_the_edit_did_not_introduce(monkeypatch):
    """A deck may carry an override its template stopped supporting after the
    save. Editing that slide's title, or moving it, is not a request for the
    scheme and is not refused - the REST PATCH's rule; only a slide theme the
    edit introduces is."""
    user_id = uuid.uuid4()
    template_id = uuid.uuid4()
    spec = _themed_deck()  # slide 5 light under a dark-only template
    existing = _existing_row(user_id, spec, template_container_id=template_id)
    session = _FakeSession(results=[*_deck_lookup(existing), *_template_lookup(_template_row(template_id))])
    _patch_session(monkeypatch, session)

    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [
                {"op": "update_slide", "slide": "5", "fields": {"title": "Thanks again"}},
                {"op": "move_slide", "slide": "5", "to": 1},
            ],
        },
    )
    assert not result.is_error, result.content
    assert existing.revision == 2
    assert existing.spec["slides"][0]["title"] == "Thanks again"
    assert existing.spec["slides"][0]["theme"] == "light"  # kept, not snapped, not refused

    # The same deck, a slide ASKED to take the unsupported scheme: refused.
    session = _FakeSession(results=[*_deck_lookup(existing), *_template_lookup(_template_row(template_id))])
    _patch_session(monkeypatch, session)
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "update_slide", "slide": "2", "fields": {"theme": "light"}}],
        },
    )
    assert result.is_error, result.content
    assert result.content.startswith("Nothing was changed: Slide 2 theme 'light': not supported by the template")
    assert existing.revision == 2
    # Slide 2 is the dark cover since the move: the refused 'light' never landed.
    assert existing.spec["slides"][1]["theme"] == "dark"


def test_edit_presentation_sets_a_supported_slide_theme_and_the_outline_marks_it(monkeypatch):
    user_id = uuid.uuid4()
    template_id = uuid.uuid4()
    existing = _existing_row(user_id, template_container_id=template_id)
    session = _FakeSession(results=[*_deck_lookup(existing), *_template_lookup(_template_row(template_id))])
    _patch_session(monkeypatch, session)

    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [
                {"op": "update_slide", "slide": "1", "fields": {"theme": "dark", "hero": "gradient", "hero_accent_word": True}}
            ],
        },
    )
    assert not result.is_error, result.content
    cover = existing.spec["slides"][0]
    assert cover["theme"] == "dark" and cover["hero"] == "gradient" and cover["hero_accent_word"] is True
    assert existing.revision == 2
    # The outline marks the slide read in its own scheme.
    assert "1. [title] Quarterly review [theme: dark]" in result.content
    assert "2. [agenda] Agenda\n" in result.content or "2. [agenda] Agenda " in result.content


def test_edit_presentation_template_change_snaps_the_slides_it_cannot_dress(monkeypatch):
    """A NEW template wins over every override it cannot dress, like it wins
    over the deck theme: the slide is snapped, counted as changed (so the
    identity check knows it moved) and named in the result."""
    user_id = uuid.uuid4()
    spec = _themed_deck()
    existing = _existing_row(user_id, spec)
    template_id = uuid.uuid4()
    session = _FakeSession(results=[*_deck_lookup(existing), *_template_lookup(_template_row(template_id))])
    _patch_session(monkeypatch, session)

    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {"presentation_id": str(existing.id), "template_id": str(template_id)},
    )
    assert not result.is_error, result.content
    assert existing.spec["theme"] == "dark"  # the deck snapped...
    assert existing.spec["slides"][4]["theme"] == "dark"  # ...and so did the light closing
    assert existing.spec["slides"][0]["theme"] == "dark"  # the dark cover was fine
    assert "Theme snapped from 'light' to 'dark'" in result.content
    assert "1 slide kept a theme of its own that the template 'XTM One Brand Deck' cannot dress" in result.content
    assert "(5: 'light' -> 'dark')" in result.content
    assert "Changed:\n  5. [closing] Thanks" in result.content
    assert "Deck theme 'dark': 3 slides inherited the change; 2 kept their own theme" in result.content


#: The covers of a template that declares the tiled ones as its own.
_TILED_COVERS = {"heroes": ["mosaic", "cascade", "horizon", "pillar"]}


def test_edit_presentation_refuses_a_cover_the_decks_template_does_not_offer(monkeypatch):
    """Setting a THREAD cover on a brand deck is refused with the slide
    and the covers the template offers, nothing written; the template that
    declares the cover takes it."""
    user_id = uuid.uuid4()
    template_id = uuid.uuid4()
    existing = _existing_row(user_id, template_container_id=template_id)
    session = _FakeSession(results=[*_deck_lookup(existing), *_template_lookup(_template_row(template_id))])
    _patch_session(monkeypatch, session)
    operations = [{"op": "update_slide", "slide": "1", "fields": {"hero": "cascade"}}]

    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(executor, "edit_presentation", {"presentation_id": str(existing.id), "operations": operations})
    assert result.is_error, result.content
    assert result.content.startswith(
        "Nothing was changed: Slide 1 hero 'cascade': a cover the template 'XTM One Brand Deck' "
        "does not offer (plain / gradient / split / spotlight)."
    )
    assert existing.revision == 1
    assert session.added == [] and not session.committed

    tiled = _template_row(template_id, name="THREAD Conference Deck", extra_metadata=_TILED_COVERS)
    session = _FakeSession(results=[*_deck_lookup(existing), *_template_lookup(tiled)])
    _patch_session(monkeypatch, session)
    result = _run_tool(executor, "edit_presentation", {"presentation_id": str(existing.id), "operations": operations})
    assert not result.is_error, result.content
    assert existing.spec["slides"][0]["hero"] == "cascade"
    assert existing.revision == 2


def test_edit_presentation_template_change_gives_each_cover_the_new_templates_default(monkeypatch):
    """Moving a THREAD deck to a brand template gives every tiled cover the
    brand's default, counts the slide as changed once even when its theme was
    snapped too, and names it in the result."""
    user_id = uuid.uuid4()
    spec = _themed_deck()  # slide 1: dark cover; slide 5: light closing
    spec["slides"][0]["hero"] = "cascade"
    spec["slides"][4]["hero"] = "mosaic"
    existing = _existing_row(user_id, spec)
    template_id = uuid.uuid4()
    brand = _template_row(
        template_id, extra_metadata={"deck_defaults": {"hero_defaults": {"title": "split", "closing": "gradient"}}}
    )
    session = _FakeSession(results=[*_deck_lookup(existing), *_template_lookup(brand)])
    _patch_session(monkeypatch, session)

    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(executor, "edit_presentation", {"presentation_id": str(existing.id), "template_id": str(template_id)})
    assert not result.is_error, result.content
    assert existing.spec["slides"][0]["hero"] == "split"
    assert existing.spec["slides"][4]["hero"] == "gradient" and existing.spec["slides"][4]["theme"] == "dark"
    assert (
        "Hero: 2 slides asked for a cover the template 'XTM One Brand Deck' does not offer; they take the "
        "default cover instead (1: 'cascade' -> 'split'; 5: 'mosaic' -> 'gradient')."
    ) in result.content
    assert "2 slides changed, 3 unchanged" in result.content
    changed = result.content.split("Changed:\n", 1)[1].split("Current slides:", 1)[0]
    assert changed.count("5. [closing]") == 1 and "1. [title]" in changed


def test_edit_presentation_update_deck_theme_reports_how_far_it_reached(monkeypatch):
    user_id = uuid.uuid4()
    existing = _existing_row(user_id, _themed_deck())
    session = _FakeSession(results=_deck_lookup(existing))
    _patch_session(monkeypatch, session)

    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {"presentation_id": str(existing.id), "operations": [{"op": "update_deck", "fields": {"theme": "dark"}}]},
    )
    assert not result.is_error, result.content
    assert "0 slides changed, 5 unchanged" in result.content
    assert "Deck theme 'dark': 3 slides inherited the change; 2 kept their own theme and were left as they are: slides 1 (dark), 5 (light)" in result.content
    assert existing.spec["slides"][0]["theme"] == "dark" and existing.spec["slides"][4]["theme"] == "light"


def test_generate_presentation_applies_the_templates_slide_defaults(monkeypatch):
    """A brand's ``deck_defaults.hero_defaults`` / ``hero_tones`` /
    ``slide_themes`` fill the title and closing slides the agent left unset -
    and only those: an explicit ``hero: plain`` wins, a body slide is never
    touched."""
    from app.services.presentations import apply_deck_defaults, template_deck_defaults

    defaults = template_deck_defaults(
        {
            "deck_defaults": {
                "theme": "light",
                "hero_defaults": {"title": "gradient", "closing": "Spotlight", "content": "gradient", "agenda": "neon"},
                "hero_tones": {"title": "Accent", "closing": "#0FBCFF"},
                "slide_themes": {"title": "dark", "closing": "sepia"},
                "transition": "fade",
            }
        }
    )
    assert defaults == {
        "theme": "light",
        "transition": "fade",
        "hero_defaults": {"title": "gradient", "closing": "spotlight"},
        "hero_tones": {"title": "accent"},
        "slide_themes": {"title": "dark"},
    }
    payload = {
        "title": "T",
        "slides": [
            {"layout": "title", "title": "Cover"},
            {"layout": "content", "title": "Body", "blocks": [{"type": "text", "content": "x"}]},
            {"layout": "closing", "hero": "plain", "theme": "light", "hero_tone": "muted"},
        ],
    }
    apply_deck_defaults(payload, defaults)
    assert payload["theme"] == "light" and payload["transition"] == "fade"
    assert payload["slides"][0]["hero"] == "gradient" and payload["slides"][0]["theme"] == "dark"
    assert payload["slides"][0]["hero_tone"] == "accent"
    assert "hero" not in payload["slides"][1] and "theme" not in payload["slides"][1]
    assert "hero_tone" not in payload["slides"][1]
    assert payload["slides"][2]["hero"] == "plain" and payload["slides"][2]["theme"] == "light"
    assert payload["slides"][2]["hero_tone"] == "muted"
    validate_spec(payload)
    # Nothing readable: no defaults at all.
    assert template_deck_defaults({"deck_defaults": {"hero_defaults": "gradient", "slide_themes": [], "hero_tones": 3}}) == {}
