"""The generate_presentation guidance is derived from, and pinned to, the spec.

An example the validator would refuse teaches the model a field that does not
exist - worse than no example. These tests run every example in the tool
description AND every JSON block in the ``presentation-design`` skill through
the real ``validate_spec``, and pin the derived sections (tones, catalog
coverage, budgets) to the constants they claim to describe.
"""

from __future__ import annotations

import json
import re
import typing
import uuid
from pathlib import Path

import pytest

from app.services import presentation_guidance as guidance
from app.services.presentation_layout import FindingKind, Severity
from app.services.presentation_legacy import RETIRED_LAYOUTS
from app.services.presentation_spec import (
    BLOCK_TYPES,
    DEFAULT_SLIDE_CAP,
    FILL_BLOCK_TYPES,
    MAX_BLOCKS_PER_SLIDE,
    MAX_LIST_ITEMS,
    MAX_SLIDE_CAP,
    MAX_TABLE_COLUMNS,
    MAX_TABLE_ROWS,
    SLIDE_LAYOUTS,
    ContentSlide,
    Hero,
    TitleSlide,
    Tone,
    spec_reference,
    validate_spec,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = BACKEND_ROOT / "app/agents/seed_data/skill_content/presentation-design.md"


# ── Examples validate ────────────────────────────────────────────────────────


@pytest.mark.parametrize("block_type", BLOCK_TYPES)
def test_every_block_type_has_a_validating_example(block_type):
    example = guidance.BLOCK_EXAMPLES[block_type]
    assert example["type"] == block_type
    spec = validate_spec({"title": "t", "slides": [{"layout": "content", "title": "x", "blocks": [example]}]})
    assert spec["slides"][0]["blocks"][0]["type"] == block_type


def test_block_examples_cover_the_catalog_in_catalog_order():
    """Coverage AND order: the description lists blocks the way the reference does."""
    assert tuple(guidance.BLOCK_EXAMPLES) == BLOCK_TYPES


@pytest.mark.parametrize("layout", SLIDE_LAYOUTS)
def test_every_layout_has_a_validating_example(layout):
    example = guidance.LAYOUT_EXAMPLES[layout]
    assert example["layout"] == layout
    spec = validate_spec({"title": "t", "slides": [example]})
    assert spec["slides"][0]["layout"] == layout


def test_layout_examples_cover_exactly_the_catalog_in_catalog_order():
    assert tuple(guidance.LAYOUT_EXAMPLES) == SLIDE_LAYOUTS
    for retired in RETIRED_LAYOUTS:
        assert retired not in guidance.LAYOUT_EXAMPLES


def test_the_arr_bridge_example_validates_adds_up_and_is_taught():
    """The one waterfall example is the board slide's ARR bridge: it
    saves, its totals are the sum of its steps (an example the validator
    refused would teach the model a broken bridge), and the tool description
    carries it as compact JSON in the CHARTS paragraph."""
    from app.services.presentation_spec import waterfall_gaps

    example = guidance.WATERFALL_CHART_EXAMPLE
    spec = validate_spec({"title": "t", "slides": [{"layout": "content", "title": "x", "blocks": [example]}]})
    block = spec["slides"][0]["blocks"][0]
    assert block["kind"] == "waterfall" and block["totals"] == [0, 4]
    assert waterfall_gaps(example["series"][0]["values"], example["totals"], 0) == []
    values = example["series"][0]["values"]
    assert sum(values[1:4]) + values[0] == values[4]
    description = guidance.build_generate_presentation_description()
    assert json.dumps(example, ensure_ascii=False, separators=(",", ":")) in description
    charts = next(part for part in description.split("\n\n") if part.startswith("CHARTS:"))
    assert "/ waterfall / butterfly / map)" in charts and "`totals`" in charts and "refused with the gap named" in charts


def test_the_butterfly_example_is_the_issue_s_pyramid_and_validates_where_a_deck_puts_it():
    """Contributors and managers per seniority level: the acceptance
    block word for word, saved on a content slide and in a dashboard panel,
    and taught with what the PPTX makes of it - an editable clustered bar
    chart, the left series negative under a 0;0; format, its categories on
    the left axis - and that a 0 draws no bar and no value, so the
    agent hands the file over knowing it."""
    example = guidance.BUTTERFLY_CHART_EXAMPLE
    assert example == {
        "type": "chart",
        "kind": "butterfly",
        "categories": ["Executive", "Director", "Manager", "Lead", "Senior", "Mid", "Entry"],
        "series": [
            {"name": "Contributors", "values": [0, 2, 10, 40, 80, 30, 15]},
            {"name": "Managers", "values": [5, 20, 18, 6, 0, 0, 0]},
        ],
        "show_values": True,
    }
    content = validate_spec({"title": "t", "slides": [{"layout": "content", "title": "x", "blocks": [example]}]})
    block = content["slides"][0]["blocks"][0]
    assert block["kind"] == "butterfly" and not {"spine", "symmetric", "show_totals"} & set(block)
    dashboard = {
        "layout": "dashboard",
        "title": "People",
        "tiles": [{"value": "177", "label": "Contributors"}, {"value": "49", "label": "Managers"}],
        "panels": [{"title": "Seniority mix", "block": example}],
    }
    validate_spec({"title": "t", "slides": [dashboard]})
    description = guidance.build_generate_presentation_description()
    assert guidance._compact(example) in description
    charts = next(part for part in description.split("\n\n") if part.startswith("CHARTS:"))
    for word in (
        "`butterfly`",
        "exactly two series",
        "never signed",
        "no bar and no value",
        "spine left",
        "show_totals",
        "clustered bar chart",
        "0;0;",
    ):
        assert word in charts, word


def test_the_map_example_validates_and_the_tool_says_the_pptx_carries_a_picture():
    """The one map example: ISO 3166-1 alpha-2 codes, one series,
    fictitious figures; it saves on a slide and in a dashboard panel, and the
    CHARTS paragraph says what a map is and that PowerPoint gets a picture of
    it with its text beside it - not an editable chart."""
    example = guidance.MAP_CHART_EXAMPLE
    assert example["kind"] == "map" and len(example["series"]) == 1
    content = validate_spec({"title": "t", "slides": [{"layout": "content", "title": "x", "blocks": [example]}]})
    assert content["slides"][0]["blocks"][0]["categories"] == example["categories"]
    dashboard = {
        "layout": "dashboard",
        "title": "Where our customers are",
        "tiles": [{"value": "133", "label": "Customers"}, {"value": "8", "label": "Countries"}],
        "panels": [{"title": "Customers by country", "span": 2, "block": example}],
    }
    validate_spec({"title": "t", "slides": [dashboard]})
    description = guidance.build_generate_presentation_description()
    charts = next(part for part in description.split("\n\n") if part.startswith("CHARTS:"))
    assert guidance._compact(example) in charts
    assert "ISO 3166-1 alpha-2 codes" in charts and "region europe / americas / apac" in charts
    assert "refused with its index named" in charts
    assert "the PPTX carries the map as a picture with its title, caption and legend as text - not an editable chart" in charts
    # The stepped legend and the markers switch, one sentence.
    assert "legend_style classes trades the ramp for at most five flat classes listed beside the map" in charts
    assert "(classes 2-7 forces the count)" in charts and "markers false drops the dot" in charts
    assert "a map takes one value per country, up to 249" in guidance.format_soft_budgets()


def test_the_frontend_chart_catalog_draws_every_chart_example_the_tool_teaches():
    """``blocks/chart-catalog.json`` is what ``chart-catalog.test.tsx`` draws
    through the real renderer: the chart examples of the tool description,
    word for word - the block example and every ``*_CHART_EXAMPLE`` - so an
    example the viewer cannot draw fails the frontend build, and the two lists
    cannot drift."""
    catalog_path = BACKEND_ROOT.parent / "frontend/src/components/presentation/blocks/chart-catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    taught = {"chart": guidance.BLOCK_EXAMPLES["chart"]}
    taught.update(
        {name.removesuffix("_EXAMPLE").lower(): value for name, value in vars(guidance).items() if name.endswith("_CHART_EXAMPLE")}
    )
    assert {"waterfall_chart", "dual_axis_chart", "butterfly_chart", "map_chart"} <= taught.keys()
    assert catalog == taught
    # The block example is served by get_presentation(reference=...) (#8812).
    description = guidance.build_generate_presentation_description() + guidance.build_presentation_reference_text()
    for example in taught.values():
        assert json.dumps(example, ensure_ascii=False, separators=(",", ":")) in description


def test_the_dual_axis_example_validates_on_a_slide_and_a_dashboard_panel():
    """New logos with ASP: the one two-unit example, taught as the
    agent writes it and validated where a board deck puts it."""
    example = guidance.DUAL_AXIS_CHART_EXAMPLE
    assert example["type"] == "chart" and example["kind"] == "combo"
    assert [series.get("axis") for series in example["series"]] == [None, "secondary"]
    assert example["secondary_format"] == "currency"
    content = validate_spec({"title": "t", "slides": [{"layout": "content", "title": "x", "blocks": [example]}]})
    assert content["slides"][0]["blocks"][0]["series"][1]["axis"] == "secondary"
    dashboard = {
        "layout": "dashboard",
        "title": "Q3 at a glance",
        "tiles": [{"value": "56", "label": "New logos"}, {"value": "EUR 540k", "label": "ASP"}],
        "panels": [{"title": "New logos and ASP", "block": example}],
    }
    validate_spec({"title": "t", "slides": [dashboard]})
    description = guidance.build_generate_presentation_description()
    assert guidance._compact(example) in description
    assert 'axis "secondary"' in description and "secondary_format" in description


def test_examples_use_the_optional_fields_that_make_a_block_worth_using():
    """A bare-minimum example teaches nothing; each should show one real choice."""
    assert guidance.BLOCK_EXAMPLES["list"]["style"] == "check"
    assert guidance.BLOCK_EXAMPLES["metric"]["trend"] == "down"
    assert guidance.BLOCK_EXAMPLES["columns"]["ratio"] == "wide_left"
    assert guidance.BLOCK_EXAMPLES["process_flow"]["loop"] is True
    assert guidance.BLOCK_EXAMPLES["spacer"]["grow"] is True
    assert guidance.LAYOUT_EXAMPLES["agenda"]["current"] == 2
    assert guidance.LAYOUT_EXAMPLES["content"]["notes"]


_PLACEHOLDER_FILE_ID = re.compile(r"[A-Z][A-Z_]*FILE_ID")


def test_the_image_example_does_not_pretend_to_know_a_file_id():
    """A fake UUID would be copied verbatim; a placeholder is visibly a placeholder.

    Upper-case tokens, not ``<...>``: the Markdown trace renderer drops a
    bracketed placeholder as an unknown tag."""
    file_id = guidance.BLOCK_EXAMPLES["image"]["file_id"]
    assert _PLACEHOLDER_FILE_ID.fullmatch(file_id), file_id


def _file_id_values(node: object):
    if isinstance(node, dict):
        for key, value in node.items():
            if (key == "file_id" or key.endswith("_file_id")) and isinstance(value, str):
                yield key, value
            elif key == "logos" and isinstance(value, list):
                # A bare string in a logo_wall group IS a file id (LogoItem coercion).
                yield from ((key, item) for item in value if isinstance(item, str))
            yield from _file_id_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from _file_id_values(item)


def test_no_example_pretends_to_know_a_file_id():
    """Every ``file_id`` / ``*_file_id`` in every example (``profiles`` portraits
    and logo tiles included) is a visible placeholder, never a UUID the
    model would copy verbatim into a deck ``verify_file_references`` then
    refuses - the wasted round trip this PR exists to remove."""
    uuid_like = re.compile(r"^[0-9a-fA-F-]{32,36}$")
    found = list(_file_id_values({**guidance.BLOCK_EXAMPLES, **guidance.LAYOUT_EXAMPLES}))
    assert any(key == "photo_file_id" for key, _ in found)
    assert any(key == "logo_file_id" for key, _ in found)
    assert any(key == "logos" for key, _ in found)
    for key, value in found:
        assert _PLACEHOLDER_FILE_ID.fullmatch(value), (key, value)
        assert not uuid_like.match(value), (key, value)


# ── Derived sections pin their sources ───────────────────────────────────────


def test_tone_semantics_cover_exactly_the_tone_literal():
    assert set(guidance.TONE_SEMANTICS) == set(typing.get_args(Tone))
    for meaning in guidance.TONE_SEMANTICS.values():
        assert meaning and not meaning.endswith(".")


def test_soft_budgets_quote_the_validator_caps():
    budgets = "\n".join(guidance.soft_budgets())
    assert str(MAX_BLOCKS_PER_SLIDE) in budgets
    assert str(MAX_LIST_ITEMS) in budgets
    assert f"{MAX_TABLE_ROWS} x {MAX_TABLE_COLUMNS}" in budgets
    # The slide cap is the template's: the default and the ceiling
    # are both named, so the agent knows the number is not a constant.
    assert f"cap {DEFAULT_SLIDE_CAP} slides, or the template's max_slides up to {MAX_SLIDE_CAP}" in budgets
    # The budgets are tighter than the caps, or they would not be budgets.
    assert "3-5 blocks" in budgets
    assert "<= 6 items" in budgets


def test_the_budget_sizes_blocks_to_their_content():
    """Blocks are sized to their content: the budget places a body that
    ends early (center or stretch, by judgement) with its takeaway as the last
    block (the former "single grid block sets fill: true" rule is gone)."""
    rules = [rule for rule in guidance.soft_budgets() if "sized to their content" in rule]
    assert len(rules) == 1
    (rule,) = rules
    assert "place a short body (vertical_align center or stretch)" in rule
    assert "takeaway callout last" in rule
    assert not any("fill: true" in budget for budget in guidance.soft_budgets())
    assert not any("single grid block" in budget for budget in guidance.soft_budgets())
    # One rule, one line, the tone of its neighbours.
    assert not rule.endswith(".")
    assert "\n" not in rule


def test_the_description_is_built_from_the_reference_plus_every_section():
    description = guidance.build_generate_presentation_description()
    # The catalogue is served on demand (#8812): the description names every
    # layout and block and how to read their fields, and no longer carries them.
    assert spec_reference() not in description
    assert guidance.format_block_examples() not in description
    assert 'get_presentation(reference=' in description
    for name in (*SLIDE_LAYOUTS, *BLOCK_TYPES):
        assert name in description, name
    assert guidance.format_layout_examples() in description
    assert guidance.format_tone_semantics() in description
    assert guidance.format_soft_budgets() in description
    # Routing first: a model that reads one paragraph still routes correctly.
    first_paragraph = description.split("\n\n")[0]
    assert "generate_file" in first_paragraph
    assert "markdown" in first_paragraph
    # The retired v1 layouts are not taught at all - not even as a
    # warning: a stored deck reads as v2, so the model never meets one.
    for retired in RETIRED_LAYOUTS:
        assert f'"layout": "{retired}"' not in description
        assert f"layout={retired}" not in description
        if retired not in BLOCK_TYPES:  # `comparison` is also a block, and that one IS taught
            assert retired not in description
    assert "never author new slides with them" not in description


#: What the generate_presentation description may cost on every generation
#: (#8812: 63,154 characters before the catalogue moved to get_presentation).
_DESCRIPTION_CHARS_CEILING = 18_000


def test_the_description_stays_under_its_ceiling():
    assert len(guidance.build_generate_presentation_description()) <= _DESCRIPTION_CHARS_CEILING


def test_the_reference_serves_the_whole_catalogue_on_demand():
    whole = guidance.build_presentation_reference("all")
    assert whole == guidance.build_presentation_reference_text()
    assert spec_reference() in whole
    assert guidance.format_layout_examples() in whole
    assert guidance.format_block_examples() in whole
    layouts, blocks, types = spec_reference().split("\n\n", 2)
    assert guidance.build_presentation_reference("layouts") == layouts
    assert guidance.build_presentation_reference("Blocks") == blocks
    assert guidance.build_presentation_reference("types") == types
    assert guidance.format_block_examples() in guidance.build_presentation_reference("examples")


def test_the_reference_answers_named_layouts_blocks_and_types_with_their_example():
    lines = {line[2:].split(":", 1)[0]: line for line in spec_reference().splitlines() if line.startswith("- ")}
    answer = guidance.build_presentation_reference("content, chart,CoverSpeaker")
    assert lines["content"] in answer and lines["chart"] in answer and lines["CoverSpeaker"] in answer
    assert json.dumps(guidance.LAYOUT_EXAMPLES["content"], ensure_ascii=False, separators=(",", ":")) in answer
    assert json.dumps(guidance.BLOCK_EXAMPLES["chart"], ensure_ascii=False, separators=(",", ":")) in answer
    unknown = guidance.build_presentation_reference("nope")
    assert unknown.startswith("Not a layout, block or type name: nope.")
    assert "content" in unknown and "chart" in unknown


async def test_get_presentation_serves_the_reference_without_a_deck():
    from app.services.builtin_tools.presentation import build_presentation_tools

    defs, executor = build_presentation_tools(None, "user")
    get = next(d for d in defs if d.name == "get_presentation")
    assert "reference" in get.input_schema["properties"]
    assert "required" not in get.input_schema
    handler = executor.handler("get_presentation")
    assert await handler(reference="kpi_row") == guidance.build_presentation_reference("kpi_row")


def test_the_description_teaches_the_in_place_edit_contract():
    description = guidance.build_generate_presentation_description()
    assert "presentation_id" in description
    assert "COMPLETE spec" in description
    assert "[[PRESENTATION:" in description
    assert "slides.3 (layout=content)" in description


def test_the_examples_are_compact_single_line_json():
    """Multi-line pretty JSON doubles the token cost of the examples."""
    for line in guidance.format_block_examples().splitlines():
        _, _, payload = line.partition(": ")
        assert json.loads(payload)
        assert "\n" not in payload and ": " not in payload.replace('": ', '":')


def test_the_tool_definition_uses_the_built_description_and_v2_schema():
    from app.services.builtin_tools.presentation import build_presentation_tools

    defs, _executor = build_presentation_tools(None, "user")
    generate = next(d for d in defs if d.name == "generate_presentation")
    assert generate.description == guidance.build_generate_presentation_description()
    slide_props = generate.input_schema["properties"]["slides"]["items"]["properties"]
    # The v2 fields are documented in the schema; the v1-only ones are gone
    # from it AND from the validator: a stored v1 slide is upgraded
    # before validation, never validated as itself.
    for field in ("blocks", "phases", "items", "speaker", "contacts", "id"):
        assert field in slide_props
    for retired_field in ("bullets", "rich_text", "left", "right", "verdict", "metrics"):
        assert retired_field not in slide_props
    assert slide_props["layout"]["enum"] == list(SLIDE_LAYOUTS)
    assert len(slide_props["layout"]["enum"]) == 8
    # The dashboard fields are documented next to the content ones.
    for field in ("tiles", "panels", "panel_columns"):
        assert field in slide_props
    assert slide_props["panel_columns"]["type"] == "integer"
    assert "2 or 3 (default 3)" in slide_props["panel_columns"]["description"]
    assert generate.input_schema["properties"]["slides"]["items"]["required"] == ["layout"]
    # Every schema property carries a type Gemini's cleaner can forward.
    for name, prop in slide_props.items():
        assert "type" in prop, name
        if prop.get("type") == "array":
            assert "type" in prop["items"], name


# ── The skill ────────────────────────────────────────────────────────────────


def _skill_json_blocks() -> list[dict]:
    text = SKILL_PATH.read_text(encoding="utf-8")
    raw_blocks = re.findall(r"```json\n(.*?)\n```", text, flags=re.DOTALL)
    assert raw_blocks, "the skill lost its examples"
    return [json.loads(raw) for raw in raw_blocks]


#: 3.1-3.13 are the compositions of a talk; 3.14-3.18 are the ones the sales
#: collateral is made of (feature matrix, editions, fact sheet, call to action,
#: personas); 3.19 is the KPI dashboard; 3.20 is the team slide
#: (`profiles`); 3.21 the framed product showcase and 3.22 the
#: challenge and its answer; 3.23-3.26 the org charts.
SKILL_COMPOSITIONS = 26


def test_the_skill_has_twenty_six_compositions_and_every_example_validates():
    text = SKILL_PATH.read_text(encoding="utf-8")
    compositions = re.findall(r"^### 3\.(\d+) ", text, flags=re.MULTILINE)
    assert [int(n) for n in compositions] == list(range(1, SKILL_COMPOSITIONS + 1))
    blocks = _skill_json_blocks()
    # One example per composition, plus the portrait-page skeleton of section 4.
    assert len(blocks) == SKILL_COMPOSITIONS + 1
    for slide in blocks:
        spec = validate_spec({"title": "t", "slides": [slide]})
        assert spec["slides"][0]["layout"] == slide["layout"]


def _block_types(slide: dict) -> set[str]:
    found: set[str] = set()
    stack = list(slide.get("blocks", []))
    for column in slide.get("columns") or []:
        stack.extend(column)
    if isinstance(slide.get("footer_block"), dict):
        stack.append(slide["footer_block"])
    while stack:
        block = stack.pop()
        found.add(block["type"])
        stack.extend(block.get("children", []))
        for side in ("left", "right"):
            if isinstance(block.get(side), dict):
                stack.extend(block[side].get("blocks", []))
    return found


def test_the_skill_teaches_the_collateral_blocks_the_way_the_collateral_uses_them():
    """The four blocks added for the sales collateral each get a
    composition, and the examples show the choices that make them worth using:
    a toned edition column with boolean cells and a footnote, exactly one
    highlighted tier, badges + key/value as a fact sheet, a banner with a
    primary action and no invented link."""
    blocks = _skill_json_blocks()
    by_types = [(_block_types(s), s) for s in blocks]

    matrix = next(
        s
        for types, s in by_types
        if "table" in types and "pricing_tiers" not in types and s.get("title", "").startswith("Community")
    )
    table = next(b for b in matrix["blocks"] if b["type"] == "table")
    assert any(isinstance(h, dict) and h.get("tone") == "accent" for h in table["headers"])
    assert any(True in row or False in row for row in table["rows"])
    assert table["footnote"]

    tiers_slide = next(s for types, s in by_types if "pricing_tiers" in types)
    tiers = next(b for b in tiers_slide["blocks"] if b["type"] == "pricing_tiers")["tiers"]
    assert 2 <= len(tiers) <= 4
    assert sum(1 for t in tiers if t.get("highlight")) == 1
    assert all(t["price"] for t in tiers)

    fact_sheets = [s for types, s in by_types if {"badges", "key_value"} <= types]
    assert len(fact_sheets) >= 2  # the composition AND the page skeleton
    for sheet in fact_sheets:
        kv = next(b for b in sheet["blocks"] if b["type"] == "key_value")
        assert kv["columns"] == 2

    banners = [b for _types, s in by_types for b in s.get("blocks", []) if b["type"] == "banner"]
    assert len(banners) >= 2  # the call to action AND the page hero
    cta = next(b for b in banners if b.get("actions"))
    assert sum(1 for a in cta["actions"] if a.get("primary")) == 1
    assert all("url" not in a for a in cta["actions"]), "the skill must not teach an invented link"

    # Personas: every card carries BOTH lists as one-liners - the composition
    # is "who it is for", and a card with only gains is a feature card.
    personas_slide = next(s for types, s in by_types if "personas" in types)
    personas = next(b for b in personas_slide["blocks"] if b["type"] == "personas")
    assert 2 <= len(personas["items"]) <= 4
    for card in personas["items"]:
        assert 2 <= len(card["pains"]) <= 3 and 2 <= len(card["gains"]) <= 3, card["name"]
        assert card["tagline"]
        assert all(len(point) <= 120 for point in card["pains"] + card["gains"]), card["name"]


def test_the_skill_teaches_portrait_pages_and_the_collateral_arcs():
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "## 4. Portrait pages (A4 / letter)" in text
    assert 'aspect_ratio: "A4"' in text and '"letter"' in text
    assert "Format follows the deliverable" in text
    for arc in ("Sales deck", "Product brief", "QBR", "Battlecard", "Case study", "Partner / integration one-pager"):
        assert f"**{arc}" in text, arc
    page = next(s for s in _skill_json_blocks() if s.get("id") == "page-1")
    types = [b["type"] for b in page["blocks"]]
    assert types[0] == "banner" and "columns" not in page
    blocks, _rows, _pages = _portrait_budget()
    assert blocks[0] <= len(types) <= blocks[1]


def _portrait_budget() -> tuple[tuple[int, int], ...]:
    """The tool description's portrait budget: blocks per page, table rows, pages."""
    (rule,) = [rule for rule in guidance.soft_budgets() if rule.startswith("portrait pages")]
    return tuple(
        tuple(int(n) for n in re.search(rf"(\d+)-(\d+) {unit}", rule).groups())
        for unit in ("blocks per page", "rows", "pages")
    )


def _skill_section(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end)]


def test_the_skill_portrait_numbers_are_the_tool_description_numbers():
    """The skill and the tool once disagreed on a page's density (6-10 blocks and
    10-12 table rows against 4-7 and 8-10) and on its closing banner (`soft`
    against `solid`): the model got two answers to one question. Every range
    the skill states for a page is read back against the tool's own budget,
    and the closing variant against the tool's PORTRAIT PAGES paragraph."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks, rows, pages = _portrait_budget()
    portrait = _skill_section(text, "## 4. Portrait pages", "## 5.")
    review = next(line for line in text.splitlines() if line.startswith("- Portrait pages:"))
    stated_blocks = re.findall(r"(\d+)-(\d+) blocks", portrait + review)
    assert stated_blocks and {tuple(map(int, r)) for r in stated_blocks} == {blocks}
    stated_rows = re.findall(r"(\d+)-(\d+) rows", portrait) + re.findall(r"(\d+)-(\d+) on a portrait page", text)
    assert len(stated_rows) == 2 and {tuple(map(int, r)) for r in stated_rows} == {rows}
    assert f"{pages[0]}-{pages[1]} pages" in portrait

    description = guidance.build_generate_presentation_description()
    closing = re.search(r'closes with a `banner` variant "(\w+)"', description).group(1)
    assert f"close with a second `banner` (`{closing}`)" in portrait
    assert f"a `{closing}` banner" in portrait
    assert "`soft`" not in portrait


def test_the_skill_teaches_predefined_assets_over_drawn_figures():
    """The brand deck ships the real diagrams; the skill must route to
    them before Mermaid, and to the template's own logo sets for a logo wall
    (by role - there are no asset libraries), with the
    same placeholder discipline as the tool description (no fake file ids)."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "asset-library" not in text
    assert 'get_template(role="partner_logo")' in text or 'role="partner_logo"' in text
    assert "Predefined assets before drawn ones" in text
    logo_walls = [s for s in _skill_json_blocks() if any(b.get("type") == "logo_grid" for b in s.get("blocks", []))]
    assert len(logo_walls) == 1
    grid = next(b for b in logo_walls[0]["blocks"] if b["type"] == "logo_grid")
    assert 6 <= len(grid["items"]) <= 12
    # `size` is the legacy alias of `logo_height`; the skill teaches the field.
    assert "size" not in grid and grid["logo_height"]
    # Grouped logos are a `logo_wall`, named wherever the skill routes logos.
    assert "a `logo_wall` when they are grouped" in guidance.build_generate_presentation_description()
    assert text.count("`logo_wall`") >= 3
    for key, value in _file_id_values(_skill_json_blocks()):
        assert _PLACEHOLDER_FILE_ID.fullmatch(value), (key, value)


def test_the_skill_sizes_blocks_to_their_content():
    """The skill's principle and its checklist say blocks are sized to
    their content and a slide that ends early is closed by a footer_block -
    the former "The body fills the slide" principle is gone."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "**Blocks are sized to their content.**" in text
    assert "The body fills the slide" not in text
    assert "a lone grid block has `fill: true`" not in text
    assert "never a card or a grid stretched to the floor" in text


def test_the_skill_keeps_a_footer_for_a_fact_and_centres_a_lone_card_grid():
    """A footer callout that restates the slide or comments on the ask is
    noise; a lone card grid is centred by the renderer, never by spacers."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "**A footer only when it adds a fact.**" in text
    assert "comments on the audience's ask" in text
    assert "**A card grid alone is centred.**" in text
    assert "never two `spacer` blocks around it" in text


def test_the_skill_teaches_the_balance_rules_and_the_visual_checklist():
    """Placement by composition, minimum content and the balance checklist of
    the review pass, named with the fields the contract and the layout check
    actually use."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "**Balance rules: the body reads finished.**" in text
    for field in ("vertical_align", "equal_height", "equal_rows", "distribute", "footer_block"):
        assert field in ContentSlide.model_fields, field
        assert f"`{field}" in text, field
    for kind in ("uneven", "floating"):
        assert kind in typing.get_args(FindingKind), kind
        assert f"`{kind}`" in text, kind
    assert "Balance checklist, on every capture of a content slide" in text
    for check in (
        "Empty bottom third",
        "Column bottoms apart",
        "Floating lone block",
        "Mostly empty cards",
        "Thin slide",
    ):
        assert f"**{check}:**" in text, check
    assert "a lone callout is never a slide" in text


def test_the_skill_places_the_body_by_judgement():
    """Placement is a judgement, not a rule: section 2 opens with it and names
    center and stretch as the two usual choices, no sentence makes one
    mandatory, the review pass checks the body is placed on purpose, and the
    short-body compositions (3.1, 3.3, 3.5, 3.8) show both placements with the
    takeaway callout as their last block, not in `footer_block`."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    section_2 = text.split("## 2. The rules that make a slide", 1)[1].split("\n## 3.", 1)[0]
    opening = section_2.lstrip().split("\n", 1)[0]
    assert opening.startswith("- **Place the body on purpose.**")
    assert '`vertical_align: "center"` usually suits' in opening
    assert '`vertical_align: "stretch"` (or `equal_height: true`) usually suits' in opening
    assert "`footer_block` is still fine" in opening
    for absolute in ("Middle is the default", "Every content slide whose body", "`center` by default"):
        assert absolute not in text, absolute
    review = text.split("## 6. Review pass before you answer", 1)[1]
    assert "- Body placed on purpose:" in review
    assert "never left floating under the title" in review
    titles = {
        "Five consoles cost the team a day a week",
        "From five consoles to one workflow",
        "Buy wins on time-to-value, build wins on control",
        "Every incident teaches the next detection",
    }
    examples = [s for s in _skill_json_blocks() if s.get("title") in titles]
    assert len(examples) == len(titles)
    placements = {slide["vertical_align"] for slide in examples}
    assert placements == {"center", "stretch"}, placements
    for slide in examples:
        assert slide["vertical_align"] in ("center", "stretch"), slide["title"]
        assert "footer_block" not in slide, slide["title"]
        assert not slide.get("distribute"), slide["title"]
        assert slide["blocks"][-1]["type"] == "callout", slide["title"]


def test_a_lone_grid_block_in_a_skill_example_fills_or_is_placed():
    """A lone grid block of an example spreads its own rows (`fill`, capped at
    a readable gap) or is placed on purpose (a single row of cards, centred),
    never left hugging the title."""
    lone = [
        s
        for s in _skill_json_blocks()
        if s["layout"] == "content" and "columns" not in s and len(s.get("blocks", [])) == 1
    ]
    grids = [(s, s["blocks"][0]) for s in lone if s["blocks"][0]["type"] in FILL_BLOCK_TYPES]
    assert grids, "the skill lost its lone-grid examples"
    for slide, block in grids:
        assert block.get("fill") is True or slide.get("vertical_align") in ("center", "end"), slide.get("title")
    assert any(block.get("fill") is True for _slide, block in grids)


def test_the_skill_keeps_mermaid_to_what_the_tool_allows():
    """The tool routes every figure but a flowchart or a sequence to a native
    block (`chart`, `timeline`, `roadmap`, `architecture`); the skill once
    recommended Mermaid pies, gantts and xy charts against it."""
    description = guidance.build_generate_presentation_description()
    assert "`diagram` is for a flowchart or sequence only" in description
    text = SKILL_PATH.read_text(encoding="utf-8")
    for mermaid_only in ("xychart-beta", "gantt", "quadrantChart", "mindmap"):
        assert mermaid_only not in text, mermaid_only
    diagrams = [
        block
        for slide in _skill_json_blocks()
        for column in [slide.get("blocks", []), *(slide.get("columns") or [])]
        for block in column
        if block["type"] == "diagram"
    ]
    assert diagrams
    for diagram in diagrams:
        assert diagram["source"].startswith(("flowchart", "sequenceDiagram")), diagram["source"][:30]


def test_the_skill_team_slide_uses_placeholders_for_people():
    """The team composition (`profiles`) shows portraits from the
    template's `people_photo` assets and facts with a logo or an icon - and
    names that are visibly placeholders, so no invented person is copied."""
    slides = [s for s in _skill_json_blocks() if "profiles" in _block_types(s)]
    assert len(slides) == 1
    profiles = next(b for b in slides[0]["blocks"] if b["type"] == "profiles")
    assert 2 <= len(profiles["items"]) <= 4
    for person in profiles["items"]:
        assert re.fullmatch(r"[A-Z][A-Z_]*", person["name"]), person["name"]
        assert _PLACEHOLDER_FILE_ID.fullmatch(person["photo_file_id"])
        assert 2 <= len(person["lines"]) <= 3
        assert all(line.get("icon") or line.get("logo_file_id") for line in person["lines"])
    assert 'role="people_photo"' in SKILL_PATH.read_text(encoding="utf-8")


def test_the_skill_names_only_real_cover_fields_and_the_layout_check():
    """Covers and the post-save layout check are taught with
    the names the contract and the tool result actually use."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    for field in ("hero", "hero_tone", "hero_accent_word"):
        assert field in TitleSlide.model_fields, field
        assert f"`{field}" in text, field
    for hero in typing.get_args(Hero):
        assert f"`{hero}`" in text, hero
    assert "`Layout check:`" in text
    assert "advisory" in typing.get_args(Severity)
    assert "An `advisory` line" in text


def test_the_skill_examples_only_use_catalog_layouts_and_teach_the_footer_callout():
    blocks = _skill_json_blocks()
    layouts = {slide["layout"] for slide in blocks}
    assert layouts <= set(SLIDE_LAYOUTS)
    # The retired v1 layouts are not named as layout ids anywhere in
    # the skill - not even as something to avoid; the tool result says so
    # when it matters. ("a dashboard capture" is English, not a layout id.)
    text = SKILL_PATH.read_text(encoding="utf-8")
    for retired in RETIRED_LAYOUTS:
        if retired not in BLOCK_TYPES:  # `comparison` lives on as a block the skill teaches
            assert f"`{retired}`" not in text, f"the skill still names the retired {retired!r} layout"
        assert f'"layout": "{retired}"' not in text
        assert f'"layout":"{retired}"' not in text
    assert {"content", "roadmap", "takeaways", "agenda"} <= layouts
    footers = [s for s in blocks if isinstance(s.get("footer_block"), dict)]
    assert len(footers) >= 4
    assert all(f["footer_block"]["type"] == "callout" for f in footers)


def test_the_skill_tone_semantics_match_the_tool_description():
    """Two sources of truth for what a tone means would let them drift."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    for tone in typing.get_args(Tone):
        assert f"`{tone}`" in text, tone
    assert "never decoration" in text
    assert "One idea per slide" in text


def test_the_skill_is_seeded_globally_and_bound_to_the_template_designer():
    from app.agents.builtin_agents import BUILTIN_SKILLS
    from app.agents.seed import _resolve_seed_content, seed_matches_platform
    from app.services.builtin_tools.skills import MAX_CATALOG_DESCRIPTION_CHARS

    skill = next(s for s in BUILTIN_SKILLS if s["slug"] == "presentation-design")
    for mode in ("copilot", "xtm_one", "dev"):
        assert seed_matches_platform(skill, mode), mode
    assert "template-designer" in skill["bind_to_agent_slugs"]
    assert skill["compatibility"] == "presentation"
    assert len(skill["description"]) <= MAX_CATALOG_DESCRIPTION_CHARS
    assert "generate_presentation" in skill["description"]
    content = _resolve_seed_content("skill_content", skill["content"])
    assert content == SKILL_PATH.read_text(encoding="utf-8")


def test_the_skill_names_every_presentation_tool_the_agent_is_given():
    """The skill once named three of the eleven deck tools: an agent
    following it rebuilt a read-only deck instead of duplicating it, never
    looked at its slides, and had no word on comments, exports or sharing.
    Every tool the builder hands an agent - export included, offered on a
    catalog listing - is named in the skill, so a new tool cannot ship
    without the craft that goes with it."""
    from app.services.builtin_tools import build_presentation_tools

    definitions, _executor = build_presentation_tools(db=None, user_id=str(uuid.uuid4()))  # type: ignore[arg-type]
    names = [definition.name for definition in definitions]
    assert "export_presentation" in names
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert [name for name in names if f"`{name}`" not in text] == []


# ── The Template Designer persona ────────────────────────────────────────────


def test_the_template_designer_knows_generate_with_ai_defaults_and_the_skill():
    from app.agents.builtin_agents import BUILTIN_AGENTS
    from app.agents.seed import _resolve_seed_content
    from app.services.presentations import DECK_DEFAULT_KEYS

    designer = next(a for a in BUILTIN_AGENTS if a["slug"] == "template-designer")
    persona = _resolve_seed_content("agent_persona", designer["persona"])
    assert "Generate with AI" in persona
    assert "deck_defaults" in persona and "guidance" in persona
    for key in DECK_DEFAULT_KEYS:
        assert f"`{key}`" in persona, key
    assert "load_skill" in persona and "Presentation Design" in persona
    assert "attach_template_asset" in persona and "`logo_light`" in persona
    # It still cannot read a palette out of an image; the UI can.
    assert "cannot read colours out of an image" in persona
