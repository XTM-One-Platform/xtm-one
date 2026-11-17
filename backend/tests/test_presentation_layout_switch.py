"""Switching a slide's layout from the editor panel.

The panel's per-slide picker asks the server to rebuild ONE slide as another
layout; ``presentation_layout_switch`` does it deterministically (carry what
fits, derive what can be read, drop the rest and SAY so), the route gates it on
the manage tier and writes nothing, and the picker's options come from the
template's advertised catalog (``supported_layouts`` on the deck response) or
the whole current catalog for a free deck.

Fakes only - no database, no browser.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.presentation_layout_switch import (
    LayoutSwitch,
    LayoutSwitchError,
    layout_label,
    switch_slide_layout,
)
from app.services.presentation_legacy import RETIRED_LAYOUTS
from app.services.presentation_spec import (
    SLIDE_LAYOUTS,
    slide_model,
    validate_spec,
)
from app.services.presentations import serialize_presentation, template_supported_layouts

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend"


def _read(rel: str) -> str:
    return (FRONTEND_ROOT / rel).read_text(encoding="utf-8")


# ── Sample slides, one per layout the picker can start from ─────────────────


def _title_slide() -> dict:
    return {
        "id": "s-title",
        "layout": "title",
        "title": "Threat landscape **2026**",
        "subtitle": "What changed and what to do about it",
        "speaker": {"name": "Ada Lovelace", "title": "Head of CTI", "credentials": ["CISSP"]},
        "event_label": "SANS CTI Summit",
        "footer": "Filigran - confidential",
        "notes": "Open with the numbers.",
    }


def _section_slide() -> dict:
    return {"id": "s-section", "layout": "section", "title": "Detection", "number": 2, "label": "Part two"}


def _agenda_slide(*, titled: bool = True) -> dict:
    slide = {
        "id": "s-agenda",
        "layout": "agenda",
        "items": [
            {"title": "Context", "description": "Where we start from"},
            {"title": "Findings", "description": "What the data says"},
            {"title": "Next steps"},
        ],
        "current": 1,
        "notes": "Keep it short.",
    }
    if titled:
        slide["title"] = "What we will cover"
    return slide


def _content_slide() -> dict:
    return {
        "id": "s-content",
        "layout": "content",
        "title": "Why detection lags",
        "subtitle": "Three causes",
        "blocks": [
            {"type": "heading", "content": "Coverage gaps", "level": 3},
            {"type": "text", "content": "Half the fleet reports **nothing** for weeks."},
            {"type": "list", "items": [{"text": "Stale rules"}, {"text": "No feedback loop"}]},
            {"type": "image", "file_id": str(uuid.uuid4()), "alt": "Coverage map"},
        ],
        "hide_footer": True,
        "footer_block": {"type": "text", "content": "Source: internal telemetry", "size": "sm"},
    }


def _roadmap_slide() -> dict:
    return {
        "id": "s-roadmap",
        "layout": "roadmap",
        "title": "Delivery plan",
        "phases": [
            {"subtitle": "Q1", "title": "Discover", "items": ["Interviews", "Inventory"]},
            {"subtitle": "Q2", "title": "Build", "items": ["Pipeline"]},
            {"subtitle": "Q3", "title": "Roll out"},
            {"subtitle": "Q4", "title": "Measure"},
        ],
    }


def _takeaways_slide() -> dict:
    return {
        "id": "s-takeaways",
        "layout": "takeaways",
        "items": [{"title": "Automate triage"}, {"title": "Close the loop", "description": "Feed detections back"}],
    }


def _closing_slide() -> dict:
    return {
        "id": "s-closing",
        "layout": "closing",
        "speaker": {"name": "Ada Lovelace", "title": "Head of CTI"},
        "contacts": [{"kind": "email", "value": "ada@example.com"}, {"kind": "web", "value": "https://example.com"}],
        "tagline": "Questions welcome",
    }


# The four RETIRED v1 shapes. The panel can still hold one: a deck
# opened before the retirement keeps its draft until the next save, and the
# picker offers the same moves on it - the switch upgrades the slide to the
# current catalog first, then converts it like any other.


def _generic_slide() -> dict:
    return {
        "id": "s-generic",
        "layout": "generic",
        "title": "Legacy notes",
        "bullets": ["One", "Two", "Three"],
        "rich_text": "<p>Some <b>bold</b> words &amp; a link</p><p>Second paragraph</p>",
        "image_file_id": str(uuid.uuid4()),
    }


def _two_columns_slide() -> dict:
    return {
        "id": "s-two",
        "layout": "two_columns",
        "title": "Before and after",
        "left": {"heading": "Before", "bullets": ["Manual", "Slow"]},
        "right": {"heading": "After", "bullets": ["Automated", "Fast"], "rich_text": "<p>Details</p>"},
    }


def _comparison_slide() -> dict:
    return {
        "id": "s-compare",
        "layout": "comparison",
        "title": "Build or buy",
        "left": {"label": "Build", "points": ["Control", "Cost"]},
        "right": {"label": "Buy", "points": ["Speed", "Support"]},
        "verdict": "Buy, then extend.",
    }


def _v1_dashboard_slide() -> dict:
    """The v1 ``dashboard`` SHAPE: ``metrics`` + ``rich_text`` under an id the
    catalog has ALIVE - recognised by shape, never by id."""
    return {
        "id": "s-dash",
        "layout": "dashboard",
        "title": "KPIs",
        "metrics": [
            {"label": "MTTD", "value": "4h", "delta": "-30%", "trend": "down"},
            {"label": "Coverage", "value": "82%", "delta": "+5%", "trend": "up"},
        ],
        "rich_text": "<p>Quarter to date</p>",
    }


def _dashboard_slide() -> dict:
    """The v2 ``dashboard`` layout: a KPI tile row over chart panels."""
    return {
        "id": "s-dashboard",
        "layout": "dashboard",
        "title": "Adoption keeps compounding",
        "subtitle": "Q4 in numbers",
        "tiles": [
            {"value": "6,250", "label": "Active instances", "delta": "+9%", "trend": "up", "sparkline": [4.8, 5.3, 6.25]},
            {"value": "38%", "label": "On 6.x", "trend": "up"},
            {"value": "112", "label": "EE trials", "tone": "success"},
        ],
        "panels": [
            {
                "title": "Active instances per quarter",
                "span": 2,
                "block": {
                    "type": "chart",
                    "kind": "line",
                    "categories": ["Q1", "Q2", "Q3", "Q4"],
                    "series": [{"name": "Instances", "values": [4800, 5300, 5700, 6250]}],
                    "target": 6000,
                },
            },
            {
                "title": "Trials by edition",
                "block": {
                    "type": "table",
                    "headers": ["Edition", "Trials"],
                    "rows": [["EE", "112"], ["CE", "64"]],
                },
            },
        ],
        "hide_footer": True,
        "notes": "Tiles first, then the line.",
    }


SAMPLES: dict[str, dict] = {
    "title": _title_slide(),
    "section": _section_slide(),
    "agenda": _agenda_slide(),
    "content": _content_slide(),
    "roadmap": _roadmap_slide(),
    "takeaways": _takeaways_slide(),
    "dashboard": _dashboard_slide(),
    "closing": _closing_slide(),
    "generic": _generic_slide(),
    "two_columns": _two_columns_slide(),
    "comparison": _comparison_slide(),
    "v1_dashboard": _v1_dashboard_slide(),
}

#: Conversions that MUST refuse: a slide with nothing to list cannot become a
#: list layout, a slide with no chart or table (or fewer than two metrics)
#: cannot become a dashboard, and no placeholder is invented to pretend
#: otherwise. Every sample but the dashboard itself lacks a figure - the v1
#: dashboard included: metrics alone make a tile row, never a panel.
EXPECTED_REFUSALS = {
    (source, target) for source in ("title", "section", "closing") for target in ("agenda", "roadmap", "takeaways")
} | {(source, "dashboard") for source in SAMPLES if source != "dashboard"}


def _deck_with(slide: dict) -> dict:
    return validate_spec({"title": "Deck", "slides": [slide]})


# ── 1. The whole matrix validates or refuses honestly ────────────────────────


@pytest.mark.parametrize("source", sorted(SAMPLES))
@pytest.mark.parametrize("target", SLIDE_LAYOUTS)
def test_every_conversion_validates_as_a_deck_or_refuses(source: str, target: str):
    """Whatever comes back is a slide ``validate_spec`` accepts inside a deck;
    the only alternative is a ``LayoutSwitchError`` in the person's words."""
    slide = SAMPLES[source]
    if (source, target) in EXPECTED_REFUSALS:
        with pytest.raises(LayoutSwitchError) as excinfo:
            switch_slide_layout(slide, target)
        message = str(excinfo.value)
        assert "assistant" in message or "title" in message, message
        return
    outcome = switch_slide_layout(slide, target)
    assert isinstance(outcome, LayoutSwitch)
    assert outcome.slide["layout"] == target
    assert outcome.slide["id"] == slide["id"], "the stable id survives - comments and links anchor on it"
    if slide.get("notes"):
        assert outcome.slide["notes"] == slide["notes"]
    deck = _deck_with(outcome.slide)
    assert deck["slides"][0]["layout"] == target
    # The account is made of sentences, never empty strings, and each note is
    # about the conversion at hand.
    for note in (*outcome.kept, *outcome.derived, *outcome.dropped):
        assert note and note == note.strip()
    if source != target:
        assert outcome.kept or outcome.derived, "moving a slide reads SOMETHING from it"


def test_same_layout_is_a_normalised_no_op():
    outcome = switch_slide_layout(_agenda_slide(), "agenda")
    assert outcome.slide["layout"] == "agenda"
    assert outcome.slide["title"] == "What we will cover"
    assert (outcome.kept, outcome.derived, outcome.dropped) == ((), (), ())
    assert outcome.as_dict() == {"slide": outcome.slide, "kept": [], "derived": [], "dropped": []}


def test_result_slide_is_json_safe():
    outcome = switch_slide_layout(_roadmap_slide(), "content")
    json.dumps(outcome.as_dict())


# ── 2. What is carried, derived and dropped ──────────────────────────────────


def test_agenda_to_takeaways_keeps_the_items_and_drops_the_highlight():
    outcome = switch_slide_layout(_agenda_slide(), "takeaways")
    assert [item["title"] for item in outcome.slide["items"]] == ["Context", "Findings", "Next steps"]
    assert outcome.slide["items"][0]["description"] == "Where we start from"
    assert outcome.slide["title"] == "What we will cover"
    assert "current" not in outcome.slide
    assert any("3 takeaways" in note for note in outcome.kept)
    assert any("Highlighted agenda item" in note for note in outcome.dropped)


def _takeaways_grid(count: int, columns: int = 3) -> dict:
    return {
        "id": "s-grid",
        "layout": "takeaways",
        "title": "Board agenda",
        "items": [{"title": f"Section {n}", "description": f"What {n} covers"} for n in range(1, count + 1)],
        "columns": columns,
    }


def test_takeaways_to_agenda_keeps_three_columns_only_when_the_grid_holds_full_rows():
    """Three agenda columns is the grid, which needs six to nine items:
    a 3-column takeaways slide with six items becomes a 3 x 2 agenda grid; one
    with four items would be refused by the validator, so the columns are dropped
    - and the account says which rule - instead of the whole switch failing."""
    grid = switch_slide_layout(_takeaways_grid(6), "agenda")
    assert grid.slide["columns"] == 3
    assert len(grid.slide["items"]) == 6
    assert not any("columns" in note for note in grid.dropped)
    # The stored spec is the validated one: the grid's defaults ride along.
    assert grid.slide["distribute"] is True and grid.slide["flow"] == "row"

    short = switch_slide_layout(_takeaways_grid(4), "agenda")
    assert "columns" not in short.slide
    assert any("3 columns" in note and "needs 6 to 9 items" in note and "lists 4" in note for note in short.dropped)
    # Two columns need no such count and travel as before.
    assert switch_slide_layout(_takeaways_grid(4, columns=2), "agenda").slide["columns"] == 2


def test_leaving_the_agenda_reports_its_grid_options():
    """`style`, `flow` and `distribute` are the agenda's own: moved to takeaways
    the items travel and the account says the grid look, the fill order and the
    packing did not - but only when they were set away from their defaults."""
    slide = {**_agenda_slide(), "columns": 2, "style": "grid", "flow": "column", "distribute": False}
    outcome = switch_slide_layout(slide, "takeaways")
    assert outcome.slide["columns"] == 2
    for key in ("style", "flow", "distribute"):
        assert key not in outcome.slide
    dropped = " | ".join(outcome.dropped)
    assert "Grid style" in dropped
    assert "Column-first fill order" in dropped
    assert "Rows packed to the top" in dropped
    # Defaults are nothing to report.
    plain = switch_slide_layout({**_agenda_slide(), "distribute": True, "flow": "row"}, "takeaways")
    assert not any(word in " | ".join(plain.dropped) for word in ("style", "fill order", "packed"))


def test_distribute_travels_between_the_two_layouts_that_have_a_run_to_spread():
    """A content slide has `distribute` too, with the opposite default:
    an agenda spreads unless told not to, a content slide packs unless told to.
    The LOOK is what travels, in all four directions: a packed agenda moved to
    content keeps it (the content default), a spread content slide moved to
    agenda keeps it (the agenda default), a packed content slide moved to
    agenda is told to pack (`distribute: false`, or the agenda would spread it
    silently), and a spread agenda moved to content spreads the one grid block
    the body becomes through the block's own `fill` - `distribute` on a lone
    block has no sibling to spread from and would draw nothing. A choice moved
    to a layout with no run to spread is dropped - said as a look, never as a
    field name. A default is nothing to report."""
    packed_to_content = switch_slide_layout({**_agenda_slide(), "distribute": False}, "content")
    assert packed_to_content.slide["distribute"] is False  # the content default IS packed, validated in
    assert packed_to_content.slide["blocks"][0]["fill"] is False
    assert "Rows packed to the top" in " | ".join(packed_to_content.kept)
    assert "packed" not in " | ".join(packed_to_content.dropped)

    spread = {**_content_slide(), "distribute": True}
    to_agenda = switch_slide_layout(spread, "agenda")
    assert to_agenda.slide["distribute"] is True  # the agenda default, validated in
    assert "Blocks spread over the body" in " | ".join(to_agenda.kept)
    assert "spread" not in " | ".join(to_agenda.dropped)

    # The two directions the defaults would otherwise flip silently.
    packed_content_to_agenda = switch_slide_layout(_content_slide(), "agenda")
    assert packed_content_to_agenda.slide["distribute"] is False
    assert "Blocks packed to the top" in packed_content_to_agenda.kept
    assert "packed" not in " | ".join(packed_content_to_agenda.dropped)
    spread_agenda_to_content = switch_slide_layout(_agenda_slide(), "content")
    (cards,) = spread_agenda_to_content.slide["blocks"]
    assert cards["type"] == "numbered_cards" and cards["fill"] is True
    assert spread_agenda_to_content.slide["distribute"] is False  # nothing for it to spread
    assert "Rows spread over the body (the block's fill)" in spread_agenda_to_content.kept
    # ...and the lone `fill` block reads back as the slide's spread, so the
    # round trip lands where it started: spread agenda -> content -> spread agenda.
    back = switch_slide_layout(spread_agenda_to_content.slide, "agenda")
    assert back.slide["distribute"] is True
    assert "Blocks spread over the body" in back.kept
    # A packed agenda round-trips too: packed -> content (fill off) -> packed.
    back_packed = switch_slide_layout(packed_to_content.slide, "agenda")
    assert back_packed.slide["distribute"] is False

    # A takeaways slide always shares the height among its cards: a spread
    # content run keeps its look there, a spread agenda's cards keep theirs
    # on the way to content (the same lone block, filled).
    to_takeaways = switch_slide_layout(spread, "takeaways")
    assert "distribute" not in to_takeaways.slide
    assert "Blocks spread over the body" in to_takeaways.kept
    assert "spread" not in " | ".join(to_takeaways.dropped)
    takeaways_to_content = switch_slide_layout(_takeaways_slide(), "content")
    assert takeaways_to_content.slide["blocks"][0]["fill"] is True
    assert "Rows spread over the body (the block's fill)" in takeaways_to_content.kept
    # The default says nothing.
    plain = switch_slide_layout({**_content_slide(), "distribute": False}, "takeaways")
    assert "spread" not in " | ".join(plain.dropped)
    assert "packed" not in " | ".join(plain.kept + plain.dropped)
    # The packed agenda's old note now names both layouts that choose packing.
    packed_to_takeaways = switch_slide_layout({**_agenda_slide(), "distribute": False}, "takeaways")
    assert "Rows packed to the top - only agenda and content slides choose that" in packed_to_takeaways.dropped
    # A spread agenda with more items than cards allow becomes a list - filled too.
    many = {**_agenda_slide(), "items": [{"title": f"Section {n}"} for n in range(1, 8)]}
    as_list = switch_slide_layout(many, "content")
    (listed,) = as_list.slide["blocks"]
    assert listed["type"] == "list" and listed["fill"] is True


def test_leaving_the_agenda_reports_the_style_that_changed_the_look():
    """The look follows the columns unless `style` overrides it, so the choice
    to report is the OVERRIDE in either direction: `list` on the 3-column grid
    is as deliberate as `grid` on two columns. A `style` that restates what the
    auto rule draws - `grid` at three columns, `list` at two - is a default said
    out loud and stays silent, like `flow: "row"`."""
    grid_items = {"items": [{"title": f"Section {n}"} for n in range(1, 7)]}
    chips_on_the_grid = switch_slide_layout({**_agenda_slide(), **grid_items, "columns": 3, "style": "list"}, "takeaways")
    assert chips_on_the_grid.slide["columns"] == 3
    assert "style" not in chips_on_the_grid.slide
    assert any("List style" in note and "only an agenda slide draws it" in note for note in chips_on_the_grid.dropped)

    restated_grid = switch_slide_layout({**_agenda_slide(), **grid_items, "columns": 3, "style": "grid"}, "takeaways")
    assert not any("style" in note for note in restated_grid.dropped)
    restated_list = switch_slide_layout({**_agenda_slide(), "columns": 2, "style": "list"}, "takeaways")
    assert not any("style" in note for note in restated_list.dropped)
    # Auto columns count too: seven items draw the grid, so `list` there is a choice.
    seven = {"items": [{"title": f"Section {n}"} for n in range(1, 8)]}
    auto_grid = switch_slide_layout({**_agenda_slide(), **seven, "style": "list"}, "takeaways")
    assert any("List style" in note for note in auto_grid.dropped)


def test_a_defaulted_title_is_not_carried_to_a_layout_with_its_own_default():
    """An untitled agenda slide SHOWS "Agenda" - the model's default, not the
    person's words. Moved to takeaways it reads "Key takeaways", not "Agenda";
    moved to content (no default) it keeps what it showed, flagged as such."""
    untitled = _agenda_slide(titled=False)
    assert "title" not in untitled
    as_takeaways = switch_slide_layout(untitled, "takeaways")
    assert as_takeaways.slide["title"] == "Key takeaways"
    assert any("Key takeaways" in note and "default" in note for note in as_takeaways.derived)
    as_content = switch_slide_layout(untitled, "content")
    assert as_content.slide["title"] == "Agenda"
    assert any("as the slide showed it" in note for note in as_content.derived)
    # A title the person typed is never second-guessed, even when it equals
    # the default.
    typed = {**untitled, "title": "Agenda"}
    assert switch_slide_layout(typed, "takeaways").slide["title"] == "Agenda"


def test_content_to_agenda_reads_items_from_the_blocks_and_says_what_it_dropped():
    """A heading and the text under it read as ONE item (title + description);
    every list entry is an item of its own; the image cannot be listed."""
    outcome = switch_slide_layout(_content_slide(), "agenda")
    titles = [item["title"] for item in outcome.slide["items"]]
    assert titles == ["Coverage gaps", "Stale rules", "No feedback loop"]
    assert outcome.slide["items"][0]["description"].startswith("Half the fleet reports")
    assert outcome.slide["title"] == "Why detection lags"
    assert outcome.slide["subtitle"] == "Three causes"
    assert outcome.slide["footer_block"]["content"] == "Source: internal telemetry"
    assert not outcome.slide.get("hide_footer")
    # The packed run travels as `distribute: false` - an agenda would
    # otherwise spread it by default - and the account says so.
    assert outcome.kept == ("Title", "Subtitle", "Blocks packed to the top", "Footer block")
    assert outcome.slide["distribute"] is False
    assert outcome.derived == ("3 items from the 4 blocks",)
    dropped = " | ".join(outcome.dropped)
    assert "Hidden footer" in dropped
    assert "1 visual block (image)" in dropped and "an agenda slide lists words" in dropped


def test_a_title_read_from_a_heading_is_not_also_the_first_item():
    """An untitled content slide lends its first heading to the title. The
    heading is then not ALSO the first item, and the paragraph under it is
    listed on its own, so the title is not repeated and no words are lost."""
    untitled = {key: value for key, value in _content_slide().items() if key not in ("title", "subtitle")}
    as_agenda = switch_slide_layout(untitled, "agenda")
    assert as_agenda.slide["title"] == "Coverage gaps"
    titles = [item["title"] for item in as_agenda.slide["items"]]
    assert titles == ["Half the fleet reports **nothing** for weeks.", "Stale rules", "No feedback loop"]
    assert "Title from the first heading block" in as_agenda.derived
    assert "3 items from the 4 blocks" in as_agenda.derived
    as_roadmap = switch_slide_layout(untitled, "roadmap")
    assert as_roadmap.slide["title"] == "Coverage gaps"
    assert as_roadmap.slide["phases"][0]["title"] == "Half the fleet reports **nothing** for weeks."
    # A heading with nothing under it simply leaves the list.
    bare = {
        "layout": "content",
        "blocks": [{"type": "heading", "content": "Plan"}, {"type": "list", "items": [{"text": "A"}, {"text": "B"}]}],
    }
    outcome = switch_slide_layout(bare, "takeaways")
    assert outcome.slide["title"] == "Plan"
    assert [item["title"] for item in outcome.slide["items"]] == ["A", "B"]


def test_a_dropped_body_is_counted_once():
    """Moved to a layout with no body, a content slide's blocks are reported
    as the body they were; the picture among them is not ALSO reported as
    unlistable - that is the account for a target that lists words."""
    outcome = switch_slide_layout(_content_slide(), "title")
    assert "4 blocks - a title slide has no body" in outcome.dropped
    assert not any("visual block" in note for note in outcome.dropped)
    listed = switch_slide_layout(_content_slide(), "takeaways")
    assert any("1 visual block (image)" in note and "lists words" in note for note in listed.dropped)


def test_roadmap_to_content_puts_the_phases_in_one_columns_block():
    """Four phases do not fit the content slide's own ``columns`` (max 3), so
    they ride a ``columns`` BLOCK (max 4) of phase cards, one per phase."""
    outcome = switch_slide_layout(_roadmap_slide(), "content")
    blocks = outcome.slide["blocks"]
    assert len(blocks) == 1 and blocks[0]["type"] == "columns"
    columns = blocks[0]["columns"]
    assert len(columns) == 4
    assert all(column[0]["type"] == "phase_card" for column in columns)
    assert [column[0]["title"] for column in columns] == ["Discover", "Build", "Roll out", "Measure"]
    assert columns[0][0]["items"] == ["Interviews", "Inventory"]
    assert outcome.slide["title"] == "Delivery plan"


def test_closing_to_title_keeps_the_speaker_and_turns_the_tagline_into_the_footer():
    outcome = switch_slide_layout(_closing_slide(), "title")
    assert [speaker["name"] for speaker in outcome.slide["speakers"]] == ["Ada Lovelace"]
    assert outcome.slide["footer"] == "Questions welcome"
    assert outcome.slide["title"] == "Thank you"  # what the closing slide showed
    assert any(note == "Speaker" for note in outcome.kept)
    assert any(note == "Footer line from the tagline" for note in outcome.derived)
    assert any(note == "2 contact lines" for note in outcome.dropped)


def test_a_title_slide_s_speakers_move_whole_to_a_title_and_as_the_lead_to_a_closing_slide():
    """A title slide names up to four speakers, a closing slide one. Moved
    to a closing slide the lead stays (words only: a closing speaker wears no
    portrait) and the others are said; moved to a content slide each speaker
    becomes a line; a stored one-speaker title reads as its list."""
    panel = {
        "id": "s-panel",
        "layout": "title",
        "title": "The panel",
        "speakers": [
            {"name": "Ada Lovelace", "title": "Head of CTI", "photo_file_id": "photo-ada"},
            {"name": "Grace Hopper", "title": "CISO"},
            {"name": "Alan Turing"},
        ],
    }
    closing = switch_slide_layout(panel, "closing")
    assert closing.slide["speaker"] == {"name": "Ada Lovelace", "title": "Head of CTI", "credentials": []}
    assert "2 more speakers - a closing slide names one" in closing.dropped
    assert "Speaker photo" in closing.dropped
    content = switch_slide_layout(panel, "content")
    lines = [block["content"] for block in content.slide["blocks"] if block["type"] == "text"]
    assert lines == ["**Ada Lovelace** - Head of CTI", "**Grace Hopper** - CISO", "**Alan Turing**"]
    assert "A text block from each speaker line" in content.derived
    legacy = {**_title_slide(), "speaker_photo_file_id": "photo-ada"}
    back = switch_slide_layout(switch_slide_layout(legacy, "closing").slide, "title")
    assert [speaker["name"] for speaker in back.slide["speakers"]] == ["Ada Lovelace"]


def test_title_to_content_derives_a_body_from_what_the_slide_shows():
    outcome = switch_slide_layout(_title_slide(), "content")
    types = [block["type"] for block in outcome.slide["blocks"]]
    assert "text" in types and "badges" in types
    speaker_line = next(block for block in outcome.slide["blocks"] if block["type"] == "text")
    assert "Ada Lovelace" in speaker_line["content"] and "Head of CTI" in speaker_line["content"]
    assert outcome.slide["title"] == "Threat landscape **2026**"  # valid markup passes as-is
    assert outcome.slide["subtitle"] == "What changed and what to do about it"


def test_a_slide_with_nothing_to_list_is_refused_not_padded():
    with pytest.raises(LayoutSwitchError) as excinfo:
        switch_slide_layout(_title_slide(), "agenda")
    message = str(excinfo.value)
    assert message.startswith("This title slide has nothing to list; an agenda slide needs at least 2 items.")
    assert "assistant" in message
    with pytest.raises(LayoutSwitchError) as excinfo:
        switch_slide_layout({"layout": "takeaways", "items": [{"title": "Only one"}]}, "roadmap")
    assert "only 1 phase to list" in str(excinfo.value)
    assert "a roadmap slide needs at least 2 phases" in str(excinfo.value)


def test_too_many_items_are_cut_and_the_cut_is_accounted_for():
    many = {
        "layout": "agenda",
        "title": "Long agenda",
        "items": [{"title": f"Item {i}"} for i in range(9)],
    }
    outcome = switch_slide_layout(many, "roadmap")
    assert len(outcome.slide["phases"]) == 4
    assert outcome.derived == ("4 phases from the 9 agenda items",)
    assert outcome.dropped == ("5 phases beyond the fourth - a roadmap slide has at most 4",)
    as_takeaways = switch_slide_layout(many, "takeaways")
    assert len(as_takeaways.slide["items"]) == 6


def _block(slide: dict, index: int, **expected) -> dict:
    """The *index*-th block of *slide*, asserted to carry *expected* (the
    validated shape also carries the model's defaults - ``align``, ``size``...)."""
    block = slide["blocks"][index]
    for key, value in expected.items():
        assert block.get(key) == value, (key, block)
    return block


def test_a_content_slide_with_only_a_subtitle_becomes_a_text_block_and_an_empty_one_is_refused():
    only_subtitle = {"layout": "section", "title": "Interlude", "subtitle": "A short pause"}
    outcome = switch_slide_layout(only_subtitle, "content")
    assert len(outcome.slide["blocks"]) == 1
    _block(outcome.slide, 0, type="text", content="A short pause", size="lg")
    assert any(note == "Body text from the subtitle" for note in outcome.derived)
    with pytest.raises(LayoutSwitchError) as excinfo:
        switch_slide_layout({"layout": "section", "title": "Bare"}, "content")
    assert "nothing for a body" in str(excinfo.value)


# ── 3. Retired v1 layouts: upgraded as sources, refused as targets ───


@pytest.mark.parametrize("retired", RETIRED_LAYOUTS)
def test_retired_layouts_are_refused_as_targets(retired: str):
    with pytest.raises(LayoutSwitchError) as excinfo:
        switch_slide_layout(_content_slide(), retired)
    message = str(excinfo.value)
    assert "retired v1 layout" in message and "A slide can be moved to:" in message
    for current in SLIDE_LAYOUTS:
        assert current in message
    assert retired not in message.split("A slide can be moved to:")[1]


@pytest.mark.parametrize("retired", RETIRED_LAYOUTS)
def test_a_retired_source_is_upgraded_first_and_the_account_says_so(retired: str):
    """A draft still wearing a v1 layout is upgraded to the v2 slide that
    says the same thing before it is moved - so the SAME target picked for the
    same words gives the same slide whether the draft was v1 or already v2,
    and the account names the step."""
    step = f"Upgraded from the retired '{retired}' layout to a content slide first"
    # Asking for the layout the upgrade lands on is the upgrade alone.
    outcome = switch_slide_layout(SAMPLES[retired], "content")
    assert outcome.slide["layout"] == "content"
    assert outcome.slide["id"] == SAMPLES[retired]["id"]
    assert outcome.kept == () and outcome.derived == (step,)
    assert "rich_text" not in json.dumps(outcome.slide)
    if "rich_text" in json.dumps(SAMPLES[retired]):
        assert any("formatting" in note and "plain text" in note for note in outcome.dropped)
    else:
        assert outcome.dropped == ()
    # Any other target is the upgrade, then the ordinary move from a content slide.
    moved = switch_slide_layout(SAMPLES[retired], "section")
    assert moved.slide["layout"] == "section"
    assert moved.slide["title"] == SAMPLES[retired]["title"]
    assert moved.derived[0] == step
    assert moved.slide == switch_slide_layout(outcome.slide, "section").slide


def test_unknown_layouts_are_refused_on_both_sides():
    with pytest.raises(LayoutSwitchError, match="Unknown layout 'poster'"):
        switch_slide_layout(_content_slide(), "poster")
    with pytest.raises(LayoutSwitchError, match="layout 'poster' is not one the platform knows"):
        switch_slide_layout({"layout": "poster", "title": "x"}, "content")
    with pytest.raises(LayoutSwitchError, match="must be an object"):
        switch_slide_layout(["not", "a", "slide"], "content")  # type: ignore[arg-type]


def test_generic_to_content_keeps_the_words_of_the_rich_text_as_plain_text():
    outcome = switch_slide_layout(SAMPLES["generic"], "content")
    types = [block["type"] for block in outcome.slide["blocks"]]
    assert types == ["list", "text", "image"]
    text = outcome.slide["blocks"][1]["content"]
    assert "Some bold words & a link" in text and "Second paragraph" in text
    assert "<" not in text, "no HTML reaches a v2 block"
    assert any("plain text block" in note for note in outcome.dropped)
    _block(outcome.slide, 2, type="image", file_id=SAMPLES["generic"]["image_file_id"])


def test_script_capable_rich_text_never_reaches_a_block():
    """The request body is the panel's DRAFT, not a stored (guarded) slide: a
    v1 rich text carrying what the guard refuses is flattened to its words
    before the target model sees it, so no tag, handler or scheme survives."""
    hostile = {
        "layout": "generic",
        "title": "Old notes",
        "rich_text": (
            '<p onmouseover="alert(1)">Quarter <script>alert(1)</script>review</p>'
            '<a href="javascript:alert(1)">link</a>'
        ),
    }
    outcome = switch_slide_layout(hostile, "content")
    text = outcome.slide["blocks"][0]
    assert text["type"] == "text"
    assert "<" not in text["content"] and ">" not in text["content"]
    assert "onmouseover" not in text["content"] and "javascript:" not in text["content"]
    assert "Quarter" in text["content"] and "review" in text["content"] and "link" in text["content"]
    validate_spec({"title": "Deck", "slides": [outcome.slide]})


def test_two_columns_to_content_keeps_the_two_sides_as_columns():
    outcome = switch_slide_layout(_two_columns_slide(), "content")
    assert not outcome.slide.get("blocks")
    columns = outcome.slide["columns"]
    assert len(columns) == 2
    assert columns[0][0]["type"] == "heading" and columns[0][0]["content"] == "Before"
    assert columns[0][0]["level"] == 3
    assert [item["text"] for item in columns[0][1]["items"]] == ["Manual", "Slow"]
    assert columns[1][-1]["type"] == "text" and columns[1][-1]["content"] == "Details"


def test_comparison_and_dashboard_become_their_v2_blocks():
    compared = switch_slide_layout(_comparison_slide(), "content")
    _block(compared.slide, 0, type="comparison")
    assert compared.slide["blocks"][0]["left"]["label"] == "Build"
    _block(compared.slide, -1, type="text", content="Buy, then extend.")

    measured = switch_slide_layout(_v1_dashboard_slide(), "content")
    grid = _block(measured.slide, 0, type="metrics_grid")
    assert [tile["label"] for tile in grid["items"]] == ["MTTD", "Coverage"]
    assert grid["items"][0]["delta"] == "-30%" and grid["items"][0]["trend"] == "down"
    _block(measured.slide, 1, type="text", content="Quarter to date")


def test_a_v1_dashboard_is_recognised_by_shape_and_upgraded_first():
    """``dashboard`` is a live layout again, so the v1 slide wearing
    that id is told apart by its SHAPE (``metrics`` / ``rich_text``, no
    ``tiles`` and no ``panels``) - and the account says which it was."""
    from app.services.presentation_legacy import is_retired_layout, is_retired_slide

    assert not is_retired_layout("dashboard")
    assert is_retired_slide(_v1_dashboard_slide()) and not is_retired_slide(_dashboard_slide())
    step = "Upgraded from the retired v1 'dashboard' shape to a content slide first"
    outcome = switch_slide_layout(_v1_dashboard_slide(), "content")
    assert outcome.slide["layout"] == "content" and outcome.slide["id"] == "s-dash"
    assert outcome.derived == (step,)
    moved = switch_slide_layout(_v1_dashboard_slide(), "section")
    assert moved.derived[0] == step and moved.slide["title"] == "KPIs"
    # A v2 dashboard is never "upgraded": the same layout is a normalised no-op.
    same = switch_slide_layout(_dashboard_slide(), "dashboard")
    assert (same.kept, same.derived, same.dropped) == ((), (), ())
    assert same.slide["panels"][0]["block"]["kind"] == "line"
    # ...and a v1 shape cannot become a v2 dashboard: it has no figure to panel.
    with pytest.raises(LayoutSwitchError, match="no chart or table"):
        switch_slide_layout(_v1_dashboard_slide(), "dashboard")


def test_dashboard_to_content_lays_the_tiles_and_panels_out_as_blocks():
    outcome = switch_slide_layout(_dashboard_slide(), "content")
    types = [block["type"] for block in outcome.slide["blocks"]]
    assert types == ["metrics_grid", "heading", "chart", "heading", "table"]
    grid = outcome.slide["blocks"][0]
    assert [tile["label"] for tile in grid["items"]] == ["Active instances", "On 6.x", "EE trials"]
    assert grid["items"][0]["sparkline"] == [4.8, 5.3, 6.25]
    _block(outcome.slide, 1, type="heading", content="Active instances per quarter", level=3)
    _block(outcome.slide, 2, type="chart", kind="line", target=6000.0)
    assert outcome.slide["subtitle"] == "Q4 in numbers" and outcome.slide["hide_footer"] is True
    assert any("3 tiles" in note and "2 panels" in note for note in outcome.derived), outcome.derived


def test_content_to_dashboard_reads_metrics_as_tiles_and_figures_as_panels():
    """A body's metrics become the tile row, its charts and tables the panels,
    and a heading right above a figure becomes that panel's title."""
    slide = {
        "id": "s-figures",
        "layout": "content",
        "title": "Q4 in figures",
        "blocks": [
            {"type": "metrics_grid", "items": [{"value": "42%", "label": "Fewer alerts"}, {"value": "11 min", "label": "Triage"}]},
            {"type": "heading", "content": "Alerts per week", "level": 3},
            {
                "type": "chart",
                "kind": "bar",
                "categories": ["W1", "W2"],
                "series": [{"name": "Alerts", "values": [120, 70]}],
            },
            {"type": "text", "content": "Same team, same budget."},
            {"type": "table", "headers": ["Team", "Alerts"], "rows": [["SOC", "70"]]},
        ],
        "footer_block": {"type": "callout", "content": "The platform did it."},
    }
    outcome = switch_slide_layout(slide, "dashboard")
    assert outcome.slide["layout"] == "dashboard"
    assert [tile["label"] for tile in outcome.slide["tiles"]] == ["Fewer alerts", "Triage"]
    panels = outcome.slide["panels"]
    assert [panel["block"]["type"] for panel in panels] == ["chart", "table"]
    assert panels[0]["title"] == "Alerts per week" and "title" not in panels[1]
    assert outcome.slide["footer_block"]["type"] == "callout"
    assert any("2 KPI tiles" in note for note in outcome.derived), outcome.derived
    assert any("2 panels" in note.lower() for note in outcome.derived), outcome.derived
    assert any("text" in note for note in outcome.dropped), outcome.dropped
    validate_spec({"title": "Deck", "slides": [outcome.slide]})
    # One metric is not a tile row: refused, with what to add.
    slide["blocks"][0]["items"].pop()
    with pytest.raises(LayoutSwitchError, match="only 1 metric"):
        switch_slide_layout(slide, "dashboard")


# ── 4. Text hygiene ──────────────────────────────────────────────────────────


def test_broken_markup_is_flattened_and_long_text_is_cut():
    stray = {"layout": "content", "title": "One ** stray marker", "blocks": [{"type": "text", "content": "Body"}]}
    outcome = switch_slide_layout(stray, "section")
    assert outcome.slide["title"] == "One stray marker"  # single-spaced, no marker

    long_title = {"layout": "content", "title": "x" * 400, "blocks": [{"type": "text", "content": "Body"}]}
    outcome = switch_slide_layout(long_title, "section")
    assert len(outcome.slide["title"]) == 300 and outcome.slide["title"].endswith("...")

    tagged = {"layout": "content", "title": "Bold <b>tag</b> text", "blocks": [{"type": "text", "content": "Body"}]}
    outcome = switch_slide_layout(tagged, "title")
    assert outcome.slide["title"] == "Bold tag text"


def test_layout_labels_read_as_prose():
    assert layout_label("two_columns") == "two columns"  # a retired id, named for the account only
    assert layout_label("takeaways") == "takeaways"
    for layout in SLIDE_LAYOUTS:
        assert "_" not in layout_label(layout)


def test_every_catalog_layout_has_a_model_and_the_retired_ones_have_none():
    for layout in SLIDE_LAYOUTS:
        assert slide_model(layout) is not None, layout
    assert slide_model("poster") is None
    for retired in RETIRED_LAYOUTS:
        assert retired not in SLIDE_LAYOUTS
        assert slide_model(retired) is None, f"{retired} is retired: no model, no destination"


def test_default_titles_are_read_off_the_models():
    """Whether a title is the layout's default or the person's is answered
    from the models' own defaults, so a renamed default cannot drift past the
    check; the roadmap's title is required, so its default is the switch's
    own. The literal values are pinned so a change is SEEN here."""
    from app.services.presentation_layout_switch import _DEFAULT_TITLES

    assert _DEFAULT_TITLES == {
        "agenda": "Agenda",
        "takeaways": "Key takeaways",
        "closing": "Thank you",
        "roadmap": "Roadmap",
    }
    for layout, default in _DEFAULT_TITLES.items():
        model = slide_model(layout)
        assert model is not None
        if layout == "roadmap":
            assert model.model_fields["title"].is_required()
        else:
            assert model.model_fields["title"].default == default
    for layout in SLIDE_LAYOUTS:
        if layout not in _DEFAULT_TITLES:
            model = slide_model(layout)
            assert model is not None
            assert not isinstance(model.model_fields["title"].default, str), layout


# ── 5. What the picker offers ────────────────────────────────────────────────


def test_template_supported_layouts_reads_the_advertised_catalog_in_catalog_order():
    # A retired id a template still advertises is skipped like an unknown one.
    advertised = [{"id": "closing"}, {"id": "content"}, {"id": "generic"}, {"id": "title"}]
    assert template_supported_layouts(advertised) == ["title", "content", "closing"]
    # Bare ids are read the same way; duplicates and unknown ids are ignored.
    assert template_supported_layouts(["agenda", "agenda", "poster", "title"]) == ["title", "agenda"]


@pytest.mark.parametrize(
    "advertised",
    [None, [], [{"id": "generic"}, {"id": "two_columns"}], [{"id": "poster"}], "not a list", [{"name": "no id"}]],
)
def test_a_template_that_advertises_nothing_renderable_restricts_nothing(advertised):
    assert template_supported_layouts(advertised) == list(SLIDE_LAYOUTS)


def _presentation(**columns):
    from app.models.presentation import Presentation

    now = datetime.now(UTC)
    return Presentation(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        title="Deck",
        theme="dark",
        slide_count=1,
        revision=1,
        spec={"title": "Deck", "theme": "dark", "slides": [{"layout": "title", "title": "Hi"}]},
        created_at=now,
        updated_at=now,
        **columns,
    )


def test_deck_response_carries_supported_layouts_never_empty_and_only_on_the_detail_shape():
    deck = _presentation()
    free = serialize_presentation(deck, permission="owner")
    assert free["supported_layouts"] == list(SLIDE_LAYOUTS)
    restricted = serialize_presentation(deck, permission="owner", supported_layouts=("content", "title", "generic"))
    assert restricted["supported_layouts"] == ["title", "content"]
    # A template whose list came out empty still offers the catalog: a blank
    # picker is never the answer.
    assert serialize_presentation(deck, permission="owner", supported_layouts=())["supported_layouts"] == list(
        SLIDE_LAYOUTS
    )
    summary = serialize_presentation(deck, permission="owner", include_spec=False)
    assert "supported_layouts" not in summary


def test_response_schema_default_matches_the_catalog():
    from app.api.v1.schemas.presentation import PresentationResponse

    assert PresentationResponse.model_fields["supported_layouts"].default == list(SLIDE_LAYOUTS)


def test_template_palette_carries_the_layouts(monkeypatch):
    """One container load feeds the palette, the schemes, the chrome AND the
    layouts - the response route reads them all off ``TemplatePalette``."""
    import app.services.presentations as svc

    container = SimpleNamespace(
        id=uuid.uuid4(),
        theme_config={"background": "#000000"},
        extra_metadata={},
        layouts=[{"id": "title"}, {"id": "content"}, {"id": "generic"}],
        name="Brand",
    )

    async def _visible(db, container_id, *, user_id, is_admin=False):
        return container

    monkeypatch.setattr(svc, "_load_visible_template", _visible)
    palette = asyncio.run(svc.resolve_template_palette(None, uuid.uuid4(), user_id=uuid.uuid4()))  # type: ignore[arg-type]
    assert palette.supported_layouts == ("title", "content")
    free = asyncio.run(svc.resolve_template_palette(None, None, user_id=uuid.uuid4()))  # type: ignore[arg-type]
    assert free.supported_layouts is None


# ── 6. The route ─────────────────────────────────────────────────────────────


class _FakeSession:
    def __init__(self):
        self.statements: list = []
        self.commits = 0

    async def execute(self, statement, *a, **k):
        self.statements.append(statement)
        raise AssertionError("the slide-layout route never touches the database beyond the permission load")

    async def commit(self):
        self.commits += 1


def _user():
    return SimpleNamespace(id=uuid.uuid4(), is_admin=False, email="ada@example.com", full_name="Ada")


def test_route_is_gated_on_the_manage_tier_and_writes_nothing(monkeypatch):
    from app.api.v1 import presentations as api
    from app.api.v1.schemas.presentation import PresentationSlideLayoutRequest

    deck = _presentation()
    calls: list[tuple[str, str]] = []

    async def _manageable(db, user, presentation_id, *, action):
        calls.append((presentation_id, action))
        return deck, []

    monkeypatch.setattr(api, "_load_manageable", _manageable)
    session = _FakeSession()
    body = PresentationSlideLayoutRequest(slide=_agenda_slide(), layout="takeaways")
    response = asyncio.run(api.switch_slide_layout_endpoint(str(deck.id), body, _user(), session))  # type: ignore[arg-type]
    assert calls == [(str(deck.id), "edit")]
    assert response.slide["layout"] == "takeaways"
    assert any("3 takeaways" in note for note in response.kept)
    assert session.statements == [] and session.commits == 0, "stateless: the save is the PATCH"


def test_route_turns_a_refusal_into_a_422_in_the_persons_words(monkeypatch):
    from app.api.v1 import presentations as api
    from app.api.v1.schemas.presentation import PresentationSlideLayoutRequest

    deck = _presentation()

    async def _manageable(db, user, presentation_id, *, action):
        return deck, []

    monkeypatch.setattr(api, "_load_manageable", _manageable)
    body = PresentationSlideLayoutRequest(slide=_title_slide(), layout="agenda")
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(api.switch_slide_layout_endpoint(str(deck.id), body, _user(), _FakeSession()))  # type: ignore[arg-type]
    assert excinfo.value.status_code == 422
    assert "an agenda slide needs at least 2 items" in excinfo.value.detail


def test_route_refuses_a_reader_before_looking_at_the_slide(monkeypatch):
    from app.api.v1 import presentations as api
    from app.api.v1.schemas.presentation import PresentationSlideLayoutRequest

    async def _manageable(db, user, presentation_id, *, action):
        raise HTTPException(status_code=403, detail="Access denied")

    monkeypatch.setattr(api, "_load_manageable", _manageable)
    body = PresentationSlideLayoutRequest(slide=_agenda_slide(), layout="takeaways")
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(api.switch_slide_layout_endpoint(str(uuid.uuid4()), body, _user(), _FakeSession()))  # type: ignore[arg-type]
    assert excinfo.value.status_code == 403


def test_route_passes_the_loaders_404_through(monkeypatch):
    """A deck this reader cannot see is a 404 from the loader - the same
    answer the PATCH gives, so the route leaks nothing about its existence."""
    from app.api.v1 import presentations as api
    from app.api.v1.schemas.presentation import PresentationSlideLayoutRequest

    async def _manageable(db, user, presentation_id, *, action):
        raise HTTPException(status_code=404, detail="Presentation not found")

    monkeypatch.setattr(api, "_load_manageable", _manageable)
    body = PresentationSlideLayoutRequest(slide=_agenda_slide(), layout="takeaways")
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(api.switch_slide_layout_endpoint(str(uuid.uuid4()), body, _user(), _FakeSession()))  # type: ignore[arg-type]
    assert excinfo.value.status_code == 404


def test_route_source_uses_the_manage_loader_and_the_422_status():
    source = (BACKEND_ROOT / "app" / "api" / "v1" / "presentations.py").read_text(encoding="utf-8")
    route = source[source.index("async def switch_slide_layout_endpoint") :]
    route = route[: route.index("\n\n\n")]
    assert '_load_manageable(db, user, presentation_id, action="edit")' in route
    assert "_load_visible" not in route
    assert "HTTP_422_UNPROCESSABLE_CONTENT" in route
    assert "save_presentation" not in route and "log_audit" not in route


# ── 7. The frontend mirrors the catalog and speaks every locale ──────────────


def test_frontend_layout_options_mirror_the_backend_catalog():
    options = _read("src/components/presentation/layout-options.ts")
    # Anchored on the declaration: the file's header names the constant in
    # prose before it is declared. ONE list: the retired v1 ids have
    # no frontend constant, no label and no renderer any more.
    catalog = re.search(r"export const SLIDE_LAYOUTS[^=]*=\s*\[([^\]]*)\]", options)
    assert catalog
    assert re.findall(r'"([a-z_]+)"', catalog.group(1)) == list(SLIDE_LAYOUTS)
    assert "LEGACY_LAYOUTS" not in options and "CURRENT_LAYOUTS" not in options
    assert "isLegacyLayout" not in options
    for retired in RETIRED_LAYOUTS:
        assert f'"{retired}"' not in options
    assert "supportedLayouts" in options


def test_frontend_panel_switches_layouts_through_the_route():
    hooks = _read("src/hooks/api/usePresentationApi.ts")
    assert "useSwitchSlideLayout" in hooks and "/slide-layout" in hooks
    panel = _read("src/components/presentation/PresentationEditorPanel.tsx")
    assert "useSwitchSlideLayout" in panel
    assert "supported_layouts" in panel
    assert "supportedLayouts" in panel
    # The converted slide lands by IDENTITY only: a result built from a slide
    # the draft no longer holds (edited, discarded, saved over, deleted
    # meanwhile) is dropped and said so, never applied over the newer draft.
    assert "draftRef.current.slides.includes(slide)" in panel
    # The converted slide is read in the draft's normal form first (the
    # server materialises the look defaults, the draft never carries them).
    assert "const converted = normalizeSlideLook(result.slide);" in panel
    assert "s === slide ? converted : s" in panel
    assert "s.id === slide.id" not in panel
    types = _read("src/types/index.ts")
    assert "supported_layouts?: SlideLayout[]" in types
    assert "SlideLayoutSwitchResult" in types
    preview = _read("src/components/presentation/previewSpec.ts")
    assert 'from "./layout-options"' in preview


def test_every_locale_has_the_layout_picker_strings():
    keys = (
        "Change the layout of this slide",
        "Change the layout of this page",
        "Layout changed",
        "Layout not changed",
        "The slide changed while its layout was being converted. Pick the layout again.",
        "The page changed while its layout was being converted. Pick the layout again.",
        "Could not change the layout",
        "Moving the slide...",
        "Moving the page...",
        # The `title` layout is named with the unit: the bare "Title" id already
        # means a person's job title elsewhere in the catalog ("Fonction").
        "Title slide",
        "Title page",
        "Section",
        "Agenda",
        "Content",
        "Roadmap",
        "Takeaways",
        "Closing",
        "Reorder, retitle, annotate, switch a layout. Ask your assistant for anything structural.",
    )
    for locale in ("en", "fr", "es", "ja"):
        catalog = _read(f"src/i18n/lang/{locale}.json")
        for key in keys:
            assert f'"{key}":' in catalog, (locale, key)
    # The picker never reads the ambiguous id.
    assert 't("Title")' not in _read("src/components/presentation/layout-options.ts")
