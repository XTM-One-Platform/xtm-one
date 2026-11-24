"""Presentation spec v2: block layouts, inline markup, icon catalog,
and the frontend catalog tripwires.

The renderer split (``frontend/src/components/presentation/{layouts,blocks}``)
mirrors the backend catalogs; a block or layout added on one side only renders
as an invisible gap on the slide. The tripwires below grep the frontend
sources for every catalog id so the build fails instead.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import get_args

import pytest

from app.services.country_codes import ISO_3166_ALPHA2
from app.services.presentation_icons import (
    LUCIDE_ICON_ALIASES,
    LUCIDE_ICON_NAMES,
    normalize_icon_name,
    suggest_icon_names,
)
from app.services.presentation_legacy import RETIRED_LAYOUTS
from app.services.presentation_markup import (
    MARKUP_SPAN_KINDS,
    InlineMarkupError,
    check_markup,
    markup_to_plain_text,
    parse_inline_markup,
)
from app.services.presentation_spec import (
    AGENDA_GRID_COLUMNS,
    AGENDA_GRID_MAX_ITEMS,
    AGENDA_GRID_MIN_ITEMS,
    BLOCK_TYPES,
    CARD_FILL_DESCRIPTION,
    CHART_FILL_DESCRIPTION,
    CHART_SIZE_DESCRIPTION,
    CONTAINER_BLOCK_TYPES,
    DASHBOARD_GRID_COLUMNS,
    DASHBOARD_PANEL_COLUMNS,
    FILL_BLOCK_TYPES,
    FILL_CARDS_DESCRIPTION,
    FILL_ROWS_DESCRIPTION,
    GAUGE_DIAMETERS,
    HERO_TONES,
    HERO_VARIANTS,
    LOGO_POSITIONS,
    MAP_DEFAULT_CLASSES,
    MAP_LEGEND_STYLES,
    MAP_REGIONS,
    MAX_AGENDA_ITEMS,
    MAX_CHART_CATEGORIES,
    MAX_MAP_CATEGORIES,
    OVERLAY_VARIANTS,
    SLIDE_LAYOUTS,
    ChartBlock,
    MapClassCount,
    PresentationSpecError,
    Size,
    auto_agenda_columns,
    auto_agenda_style,
    block_catalog,
    fill_description,
    layout_catalog,
    resolved_slide_scheme,
    slide_theme_overrides,
    spec_reference,
    validate_spec,
)
from app.services.presentations import THEME_CONFIG_KEYS

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend"
PRESENTATION_DIR = FRONTEND_ROOT / "src" / "components" / "presentation"


def _read(*parts: str) -> str:
    return (PRESENTATION_DIR.joinpath(*parts)).read_text(encoding="utf-8")


def _deck(*slides: dict, **extra) -> dict:
    return {"title": "Deck", "slides": list(slides), **extra}


# ── Inline markup ───────────────────────────────────────────────────────────


def test_markup_parses_every_construct():
    segments = parse_inline_markup("a **b** `c` [[accent:d]] _e_\nf")
    kinds = [(s.kind, s.text, s.tone) for s in segments]
    assert kinds == [
        ("text", "a ", None),
        ("bold", "b", None),
        ("text", " ", None),
        ("code", "c", None),
        ("text", " ", None),
        ("span", "d", "accent"),
        ("text", " ", None),
        ("em", "e", None),
        ("br", "", None),
        ("text", "f", None),
    ]


def test_markup_emphasis_is_word_bounded():
    """``snake_case_names`` must stay literal — the underscore is not markup."""
    segments = parse_inline_markup("use snake_case_names here")
    assert [s.kind for s in segments] == ["text"]


def test_markup_rejects_html_and_nesting():
    with pytest.raises(InlineMarkupError):
        parse_inline_markup("<b>bold</b>")
    with pytest.raises(InlineMarkupError):
        parse_inline_markup("**bold [[accent:inside]]**")
    with pytest.raises(InlineMarkupError):
        parse_inline_markup("[[nope:unknown tone]]")
    # The Pydantic-facing wrapper surfaces the same message as a ValueError.
    with pytest.raises(ValueError, match="HTML is not allowed"):
        check_markup("<b>bold</b>")
    assert check_markup(None) is None
    assert check_markup("**ok**") == "**ok**"


def test_markup_plain_text_strips_markers():
    assert markup_to_plain_text("**a** `b` [[gradient:c]]") == "a b c"
    assert markup_to_plain_text(None) == ""


def test_markup_span_kinds_are_the_six_tones_plus_gradient():
    assert set(MARKUP_SPAN_KINDS) == {
        "accent",
        "info",
        "success",
        "warning",
        "error",
        "muted",
        "gradient",
    }


# ── Icons ───────────────────────────────────────────────────────────────────


def test_icon_catalog_is_large_and_kebab_case():
    assert len(LUCIDE_ICON_NAMES) > 1000
    assert all(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", n) for n in LUCIDE_ICON_NAMES)
    assert "shield-check" in LUCIDE_ICON_NAMES


def test_icon_normalisation_accepts_common_spellings():
    assert normalize_icon_name("ShieldCheck") == "shield-check"
    assert normalize_icon_name("shield_check") == "shield-check"
    assert normalize_icon_name(" shield-check ") == "shield-check"
    assert normalize_icon_name("definitely-not-an-icon-xyz") is None
    assert normalize_icon_name(42) is None


def test_icon_aliases_canonicalise_to_the_file_the_catalog_exports():
    """The catalog module exports canonical names only; an alias stored as-is
    would resolve to nothing on the slide."""
    assert LUCIDE_ICON_ALIASES, "lucide ships aliases; the generator lost them"
    assert set(LUCIDE_ICON_ALIASES.values()) <= LUCIDE_ICON_NAMES
    assert not (set(LUCIDE_ICON_ALIASES) & LUCIDE_ICON_NAMES)
    alias, target = next(iter(sorted(LUCIDE_ICON_ALIASES.items())))
    assert normalize_icon_name(alias) == target
    assert normalize_icon_name(alias.replace("-", "_")) == target


def test_icon_suggestions_help_a_near_miss():
    suggestions = suggest_icon_names("sheild-check")
    assert "shield-check" in suggestions


def _lucide_icons_index() -> Path | None:
    path = FRONTEND_ROOT / "node_modules" / "lucide-react" / "dist" / "esm" / "icons" / "index.js"
    return path if path.is_file() else None


def test_icon_allow_list_matches_the_installed_catalog_module():
    """Every canonical id must be an export of the module ``deck-icons.ts``
    lazy-loads, under the PascalCase name ``lucideExportName`` derives."""
    index = _lucide_icons_index()
    if index is None:
        pytest.skip("frontend/node_modules is not installed")
    exported = set(re.findall(r"export \{ default as ([A-Za-z0-9]+) \}", index.read_text("utf-8")))
    assert len(exported) > 1000

    def pascal(name: str) -> str:
        return "".join(part[:1].upper() + part[1:] for part in name.split("-"))

    missing = sorted(name for name in LUCIDE_ICON_NAMES if pascal(name) not in exported)
    assert not missing, f"allow-list names the catalog module cannot draw: {missing[:5]}"
    extra = sorted(exported - {pascal(name) for name in LUCIDE_ICON_NAMES})
    assert not extra, f"catalog exports missing from the allow-list (rerun the generator): {extra[:5]}"


# ── Spec: v2 layouts ────────────────────────────────────────────────────────


def test_the_catalog_is_one_generation_of_layouts():
    """The v1 layouts are retired - not a model, not advertised, not in
    the catalog the tool schema, the picker and the seed all read. The
    ``dashboard`` id has a v2 model of its own (tiles + panels); the v1 shape
    under that id is still upgraded, told apart by shape (``is_retired_slide``)."""
    assert SLIDE_LAYOUTS == ("title", "section", "agenda", "content", "roadmap", "takeaways", "dashboard", "closing")
    assert not set(SLIDE_LAYOUTS) & set(RETIRED_LAYOUTS)
    assert set(layout_catalog()) == set(SLIDE_LAYOUTS)


def test_full_v2_deck_validates_and_gets_slide_ids():
    spec = validate_spec(
        _deck(
            {
                "layout": "title",
                "title": "**Threat** landscape",
                "event_label": "All hands",
                "speaker": {"name": "Ada", "title": "CTO", "credentials": ["OSCP"]},
                "icon_strip": ["shield-check", "radar"],
            },
            {"layout": "agenda", "items": [{"title": "One"}, {"title": "Two"}], "current": 2},
            {"layout": "section", "number": 1, "title": "Context"},
            {
                "layout": "content",
                "title": "Findings",
                "blocks": [
                    {"type": "text", "content": "Plain _emphasis_"},
                    {
                        "type": "list",
                        "style": "check",
                        "items": [{"text": "a"}, {"text": "b", "tone": "warning"}],
                    },
                    {
                        "type": "callout",
                        "tone": "info",
                        "title": "Note",
                        "children": [{"type": "text", "content": "inside"}],
                    },
                ],
                "footer_block": {"type": "quote", "content": "q", "attribution": "x"},
            },
            {
                "layout": "roadmap",
                "title": "Plan",
                "phases": [
                    {"title": "Q1", "items": ["a"], "goal": {"label": "Goal", "value": "x"}},
                    {"title": "Q2", "items": ["b"]},
                ],
            },
            {"layout": "takeaways", "items": [{"title": "Remember"}, {"title": "Act"}]},
            {"layout": "closing", "contacts": [{"kind": "email", "value": "a@b.co"}]},
            theme="filigran",
            chrome={"event_label": "All hands", "show_slide_numbers": True},
        )
    )
    assert [s["layout"] for s in spec["slides"]] == [
        "title",
        "agenda",
        "section",
        "content",
        "roadmap",
        "takeaways",
        "closing",
    ]
    ids = [s["id"] for s in spec["slides"]]
    assert all(ids) and len(set(ids)) == len(ids)
    assert spec["chrome"]["event_label"] == "All hands"


def test_slide_ids_are_kept_when_supplied_and_deduplicated():
    spec = validate_spec(
        _deck(
            {"layout": "title", "title": "A", "id": "intro"},
            {"layout": "section", "title": "B", "id": "intro"},
            {"layout": "closing"},
        )
    )
    ids = [s["id"] for s in spec["slides"]]
    assert ids[0] == "intro"
    assert len(set(ids)) == 3


# ── Logo placement ──────────────────────────────────────────────────


def test_logo_position_defaults_to_auto_and_accepts_every_placement():
    """The corner is a choice on the opening and closing slides only; the deck
    says where, ``auto`` follows the slide."""
    assert LOGO_POSITIONS == ("auto", "start", "center", "end")
    spec = validate_spec(_deck({"layout": "title", "title": "A"}))
    assert spec["chrome"]["logo_position"] == "auto"
    for position in LOGO_POSITIONS:
        spec = validate_spec(
            _deck({"layout": "title", "title": "A"}, chrome={"logo_position": position})
        )
        assert spec["chrome"]["logo_position"] == position
    # The other chrome keys keep their defaults when only the position is set.
    assert spec["chrome"]["show_logo"] is True


@pytest.mark.parametrize("bad", ["left", "right", "bottom-right", "", "Auto", True, 1])
def test_logo_position_refuses_anything_else(bad):
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(_deck({"layout": "title", "title": "A"}, chrome={"logo_position": bad}))
    assert "logo_position" in str(exc.value)


def test_frontend_logo_position_union_mirrors_the_backend_literal():
    """``DeckLogoPosition`` is what the editor panel offers and the renderer
    resolves; a value accepted on one side only is either refused on save or
    silently drawn in the corner."""
    types = (FRONTEND_ROOT / "src" / "types" / "index.ts").read_text(encoding="utf-8")
    match = re.search(r"export type DeckLogoPosition = ([^;]+);", types)
    assert match, "DeckLogoPosition missing from frontend/src/types/index.ts"
    union = tuple(re.findall(r'"([a-z]+)"', match.group(1)))
    assert union == LOGO_POSITIONS
    assert "logo_position?: DeckLogoPosition;" in types
    # The picker walks the same list, in the same order.
    preview = _read("previewSpec.ts")
    assert 'DECK_LOGO_POSITIONS: readonly DeckLogoPosition[] = ["auto", "start", "center", "end"]' in preview


def test_frontend_chrome_places_the_logo_per_slide_and_the_panel_offers_the_choice():
    """The deck-wide value is resolved PER SLIDE: body and section slides keep
    the corner (`end`), the opening slide follows its `align`, the closing slide
    centres. `start` is DOM order; `center` needs the stylesheet's grid, whose
    equal side columns keep the logo on the axis whatever the number measures."""
    resolver = _read("logo-position.ts")
    assert "export function resolveLogoPosition(" in resolver
    assert 'new Set(["title", "closing"])' in resolver
    assert 'if (!PLACEABLE_LAYOUTS.has(slide.layout)) return "end";' in resolver
    assert 'if (position && position !== "auto") return position;' in resolver
    assert 'return slide.align === "center" ? "center" : "start";' in resolver
    chrome = _read("chrome.tsx")
    assert 'import { resolveLogoPosition } from "./logo-position";' in chrome
    assert "resolveLogoPosition(chrome?.logo_position, slide)" in chrome
    assert "data-logo-position={logoPosition}" in chrome
    # The logo has one slot per placement; the corner slot is the right-hand
    # group's, as on every other slide.
    assert '{logoPosition === "start" ? logo : null}' in chrome
    assert '{logoPosition === "center" ? logo : null}' in chrome
    assert '{logoPosition === "end" ? logo : null}' in chrome
    css = _read("presentation.css")
    assert '.deck-root .deck-chrome[data-logo-position="center"] {' in css
    assert "grid-template-columns: 1fr auto 1fr;" in css
    assert '.deck-root .deck-chrome[data-logo-position="center"] > .deck-chrome-logo' in css
    assert '.deck-root .deck-chrome[data-logo-position="center"] > .deck-chrome-right' in css
    panel = _read("PresentationEditorPanel.tsx")
    assert 'id="deck-chrome-logo-position"' in panel
    assert "DECK_LOGO_POSITIONS.map(" in panel
    # ``auto`` is the default and drops its key, like a switch back at ``true``,
    # so an untouched deck never offers a Save that writes nothing. The
    # normaliser lives with the draft since the editor session moved out of
    # the panel; the panel writes every chrome edit through it.
    session = _read("presentation-editor.ts")
    assert 'if (key === "logo_position" && value === "auto") continue;' in session
    assert "normalizeChrome({ ...chrome, ...patch } as DeckChrome)" in panel


def test_content_slide_refuses_blocks_and_columns_together():
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(
            _deck(
                {
                    "layout": "content",
                    "blocks": [{"type": "text", "content": "a"}],
                    "columns": [[{"type": "text", "content": "b"}]],
                }
            )
        )
    assert "columns" in str(exc.value)


def test_agenda_current_must_point_at_an_item():
    with pytest.raises(PresentationSpecError):
        validate_spec(_deck({"layout": "agenda", "items": [{"title": "One"}], "current": 3}))


# ── Agenda grid ────────────────────────────────────────────────────


def _agenda(count: int, **extra) -> dict:
    items = [{"title": f"Section {n}", "description": f"What section {n} covers."} for n in range(1, count + 1)]
    return {"layout": "agenda", "items": items, **extra}


def test_agenda_grid_constants_are_the_full_rows_rule():
    """Three columns need a second row the items fill (six) and stop reading as
    a grid past three rows (nine): six to nine items and nothing else. Ten - the
    item cap - takes two columns of five."""
    assert (AGENDA_GRID_COLUMNS, AGENDA_GRID_MIN_ITEMS, AGENDA_GRID_MAX_ITEMS) == (3, 6, 9)
    assert AGENDA_GRID_MAX_ITEMS < MAX_AGENDA_ITEMS


@pytest.mark.parametrize(
    ("count", "columns"),
    [(2, 1), (3, 1), (4, 1), (5, 2), (6, 2), (7, 3), (8, 3), (9, 3), (10, 2)],
)
def test_auto_agenda_columns_follows_the_documented_rule(count, columns):
    assert auto_agenda_columns(count) == columns
    assert auto_agenda_style(columns) == ("grid" if columns == 3 else "list")


@pytest.mark.parametrize("count", [6, 7, 8, 9])
def test_agenda_accepts_three_columns_for_six_to_nine_items(count):
    spec = validate_spec(_deck(_agenda(count, columns=3)))
    slide = spec["slides"][0]
    assert slide["columns"] == 3
    # The value defaults are STORED, so a deck reads the same whatever the
    # renderer's own fallbacks become.
    assert slide["distribute"] is True
    assert slide["flow"] == "row"
    # ``style`` stays absent when omitted: the renderer resolves it from the
    # columns, and a stored ``"grid"`` would pin a 2-column deck to the grid
    # look the day the auto rule moves.
    assert "style" not in slide


@pytest.mark.parametrize("count", [2, 5, 10])
def test_agenda_refuses_three_columns_outside_the_grid_range(count):
    """The error names the slide, the layout and the rule, so the agent knows to
    move to two columns or split the agenda rather than drop items."""
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(_deck({"layout": "title", "title": "A"}, _agenda(count, columns=3)))
    message = str(exc.value)
    assert "slides.1 (layout=agenda)" in message
    assert "columns=3 needs 6 to 9 items" in message
    assert f"this agenda has {count}" in message
    human = exc.value.problems[0].for_human()
    assert human.startswith("Slide 2 (agenda)")
    assert "columns=3 needs 6 to 9 items" in human


@pytest.mark.parametrize("columns", [4, 0, "3", "three"])
def test_agenda_columns_is_one_two_or_three(columns):
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(_deck(_agenda(6, columns=columns)))
    assert "columns" in str(exc.value)


def test_agenda_grid_options_are_stored_and_bounded():
    spec = validate_spec(_deck(_agenda(6, columns=2, style="grid", distribute=False, flow="column", current=4)))
    slide = spec["slides"][0]
    assert slide["style"] == "grid"
    assert slide["distribute"] is False
    assert slide["flow"] == "column"
    assert slide["current"] == 4
    for bad in ({"style": "cards"}, {"flow": "diagonal"}, {"distribute": "sometimes"}):
        with pytest.raises(PresentationSpecError) as exc:
            validate_spec(_deck(_agenda(6, **bad)))
        assert next(iter(bad)) in str(exc.value)


def test_agenda_without_columns_still_validates_at_every_count():
    """Omitting ``columns`` is never an error: the auto rule is the renderer's,
    and a deck stored before the grid existed keeps validating unchanged."""
    for count in range(2, MAX_AGENDA_ITEMS + 1):
        slide = validate_spec(_deck(_agenda(count)))["slides"][0]
        assert "columns" not in slide
        assert slide["distribute"] is True and slide["flow"] == "row"


def test_agenda_catalog_documents_the_grid_options():
    """The tool reference is derived from the model, so the agent reads the
    columns rule, the two looks and the two switches where it reads the rest."""
    fields = layout_catalog()["agenda"]
    grid_fields = [name for name in fields if name in ("columns", "style", "distribute", "flow")]
    assert grid_fields == ["columns", "style", "distribute", "flow"]
    assert fields.index("flow") < fields.index("current")
    agenda_line = next(line for line in spec_reference().splitlines() if line.startswith("- agenda:"))
    assert "columns?: 1|2|3" in agenda_line
    assert "3 needs 6-9 items" in agenda_line
    assert 'style?: "list"|"grid"' in agenda_line
    assert "distribute?: bool = true" in agenda_line
    assert 'flow?: "row"|"column" = "row"' in agenda_line


def test_generate_presentation_tool_carries_the_agenda_grid_rule():
    """What the agent actually receives, not the reference alone: the flat
    per-slide JSON schema names the two looks and the two switches, and the tool
    description (which embeds ``spec_reference()``) carries the columns rule.
    ``columns`` is deliberately NOT a schema property - it is an integer on
    agenda and takeaways slides and an array of block arrays on content slides,
    and the flat schema has one slot per name - so the slide object stays open
    for it and the rule lives where the agent reads the rest of the spec."""
    import uuid

    from app.services.builtin_tools import build_presentation_tools

    defs, _executor = build_presentation_tools(db=None, user_id=str(uuid.uuid4()), conversation_id=None)  # type: ignore[arg-type]
    tool = next(d for d in defs if d.name == "generate_presentation")
    slide = tool.input_schema["properties"]["slides"]["items"]
    props = slide["properties"]
    assert props["style"]["enum"] == ["list", "grid"]
    assert props["distribute"]["type"] == "boolean"
    assert props["flow"]["enum"] == ["row", "column"]
    for name in ("style", "distribute", "flow"):
        assert props[name]["description"].startswith("agenda:"), name
    assert "columns" not in props
    assert "additionalProperties" not in slide
    # The rule is served by get_presentation(reference="agenda") (#8812).
    from app.services.presentation_guidance import build_presentation_reference

    assert "get_presentation(reference=" in tool.description
    assert "columns?: 1|2|3" in build_presentation_reference("agenda")
    assert "3 needs 6-9 items" in build_presentation_reference("agenda")


def test_frontend_agenda_layout_mirrors_the_backend_rule():
    """The renderer resolves an omitted ``columns`` / ``style`` with the SAME
    thresholds the backend documents on the model: a deck the agent left on
    auto must draw the grid exactly where the tool description said it would."""
    types = (FRONTEND_ROOT / "src" / "types" / "index.ts").read_text(encoding="utf-8")
    assert "columns?: 1 | 2 | 3;" in types
    assert 'export type DeckAgendaStyle = "list" | "grid";' in types
    assert 'export type DeckAgendaFlow = "row" | "column";' in types
    assert "style?: DeckAgendaStyle;" in types
    assert "distribute?: boolean;" in types
    assert "flow?: DeckAgendaFlow;" in types
    helper = _read("agenda-layout.ts")
    assert f"export const AGENDA_GRID_COLUMNS = {AGENDA_GRID_COLUMNS};" in helper
    assert f"export const AGENDA_GRID_MIN_ITEMS = {AGENDA_GRID_MIN_ITEMS};" in helper
    assert f"export const AGENDA_GRID_MAX_ITEMS = {AGENDA_GRID_MAX_ITEMS};" in helper
    assert "if (count <= 4) return 1;" in helper
    assert "if (count <= 6) return 2;" in helper
    assert "if (count <= AGENDA_GRID_MAX_ITEMS) return AGENDA_GRID_COLUMNS;" in helper
    assert "return 2;" in helper
    assert 'agendaColumns(slide) === AGENDA_GRID_COLUMNS ? "grid" : "list"' in helper
    assert "distribute: slide.distribute !== false," in helper
    # The fill order is decided when the rows are grouped, not by the grid:
    # row-major chunks, or the column-first index for `flow: "column"`.
    assert "export function agendaRowGroups(" in helper
    assert 'flow === "column" ? column * rows + row : row * columns + column' in helper
    body = _read("layouts", "body.tsx")
    assert 'import { resolveAgendaLayout } from "../agenda-layout";' in body
    for attribute in ("data-columns", "data-style", "data-flow", "data-distribute", "data-descriptions"):
        assert f"{attribute}=" in body, attribute
    # Explicit rows: an `li` per row demoted to presentation, a `listitem` per
    # cell, so the subgrid has a row to join and the list stays a list.
    assert 'className="deck-agenda-row" key={rowIndex} role="presentation"' in body
    assert 'role="listitem"' in body
    # `flow: "column"` transposes the DOM rows (1 4 7 / 2 5 8 / 3 6 9): the
    # list then `aria-owns` its cells in ITEM order, so assistive technology
    # reads 1, 2, 3... down the columns the way the eye does.
    assert 'aria-owns={transposed ? slide.items.map((_, index) => cellId(index)).join(" ") : undefined}' in body
    assert "id={transposed ? cellId(index) : undefined}" in body
    # Number, separator, title and description are separate elements: the
    # editable PPTX reads the DOM and gives each one its own text box.
    for class_name in ("deck-agenda-head", "deck-agenda-number", "deck-agenda-sep", "deck-agenda-title", "deck-agenda-desc"):
        assert f'className="{class_name}"' in body, class_name
    assert 'data-empty="true" aria-hidden="true"' in body
    css = _read("presentation.css")
    # Rows spread over the body; every cell joins its row's two tracks (head,
    # description) through a subgrid and sits its children on the tracks' top
    # edges: the rules behind "the titles of a row share a baseline", "so do
    # the descriptions" and "the grid fills the slide".
    assert '.deck-root .deck-agenda[data-distribute="true"] {\n  justify-content: space-between;' in css
    row_rule = css.split(".deck-root .deck-agenda-row {", 1)[1].split("}", 1)[0]
    assert "  grid-template-rows: auto auto;" in row_rule
    assert "  align-items: start;" in row_rule
    item_rule = css.split(".deck-root .deck-agenda-item {", 1)[1].split("}", 1)[0]
    assert "  grid-template-rows: subgrid;" in item_rule
    assert "  grid-row: span 2;" in item_rule
    assert "  align-items: start;" in item_rule
    assert (
        '.deck-root .deck-agenda[data-columns="3"] .deck-agenda-row {\n  grid-template-columns: repeat(3, minmax(0, 1fr));'
        in css
    )
    assert "-webkit-line-clamp: 2;" in css.split('.deck-root .deck-agenda[data-style="grid"] .deck-agenda-desc {', 1)[1]
    # Theme tokens only: the grid look is drawn from the deck's palette
    # (comments stripped first - an issue reference is not a hex colour).
    agenda_css = css.split("/* ── Agenda", 1)[1].split("/* ── Text blocks", 1)[0]
    agenda_rules = re.sub(r"/\*.*?\*/", "", agenda_css, flags=re.DOTALL)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(", agenda_rules), "hard-coded colour in the agenda styles"
    # The strings above pin the SOURCE; the rule is also EXECUTED, without a
    # page, by the vitest table test (like `logo-position.test.ts`), which
    # runs the same thresholds table as `test_auto_agenda_columns_follows_the_
    # documented_rule` and walks every count x columns x flow through
    # `agendaRowGroups`.
    spec = (FRONTEND_ROOT / "tests_unit" / "agenda-layout.test.ts").read_text(encoding="utf-8")
    assert "from '../src/components/presentation/agenda-layout'" in spec
    for name in ("autoAgendaColumns", "agendaStyle", "agendaRowGroups", "resolveAgendaLayout"):
        assert f"{name}(" in spec, name
    assert "[2, 1], [3, 1], [4, 1], [5, 2], [6, 2], [7, 3], [8, 3], [9, 3], [10, 2]" in spec


def test_unknown_icon_is_a_precise_error_with_suggestions():
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(
            _deck(
                {
                    "layout": "content",
                    "blocks": [{"type": "heading", "content": "x", "icon": "sheild-check"}],
                }
            )
        )
    message = str(exc.value)
    assert "sheild-check" in message
    assert "shield-check" in message


def test_html_in_markup_field_is_refused_at_generation_time():
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(_deck({"layout": "section", "title": "<b>Context</b>"}))
    assert "**bold**" in str(exc.value)


def test_nesting_is_capped_by_construction():
    """columns → callout → text is the deepest legal chain; a callout cannot
    hold a columns block and a column cannot hold another columns block."""
    with pytest.raises(PresentationSpecError):
        validate_spec(
            _deck(
                {
                    "layout": "content",
                    "blocks": [
                        {
                            "type": "callout",
                            "children": [{"type": "columns", "columns": [[]]}],
                        }
                    ],
                }
            )
        )
    with pytest.raises(PresentationSpecError):
        validate_spec(
            _deck(
                {
                    "layout": "content",
                    "blocks": [
                        {
                            "type": "columns",
                            "columns": [[{"type": "columns", "columns": [[]]}]],
                        }
                    ],
                }
            )
        )


def test_diagram_source_guard():
    ok = validate_spec(
        _deck(
            {
                "layout": "content",
                "blocks": [{"type": "diagram", "source": "flowchart LR\n  A --> B"}],
            }
        )
    )
    assert ok["slides"][0]["blocks"][0]["type"] == "diagram"
    for bad in (
        "%%{init: {'theme': 'x'}}%%\nflowchart LR\n A --> B",
        'flowchart LR\n A --> B\n click A href "https://x"',
        "flowchart LR\n A[<script>alert(1)</script>]",
        "not a diagram at all",
    ):
        with pytest.raises(PresentationSpecError):
            validate_spec(_deck({"layout": "content", "blocks": [{"type": "diagram", "source": bad}]}))


# ── Architecture block ──────────────────────────────────────────────


def _architecture(**overrides: object) -> dict:
    """The slide-11 shape: a hub band over a platform panel with a core in the middle."""
    block = {
        "type": "architecture",
        "nodes": [
            {"id": "hub", "label": "XTM Hub", "row": 1, "col": 1, "col_span": 3, "chips": ["trials", "libraries"]},
            {"id": "cti", "label": "OpenCTI", "row": 2, "col": 2, "icon": "shield"},
            {"id": "core", "label": "XTM One", "row": 3, "col": 2, "shape": "circle", "emphasis": "core"},
            {"id": "crq", "label": "OpenCRQ", "row": 4, "col": 1},
            {"id": "aev", "label": "OpenAEV", "row": 4, "col": 3},
        ],
        "edges": [
            {"source": "hub", "target": "cti", "label": "onboards"},
            {"source": "cti", "target": "crq", "label": "Threat landscape"},
            {"source": "cti", "target": "aev", "label": "Threat data & payloads"},
            {"source": "aev", "target": "cti", "label": "Security posture"},
            {"source": "core", "target": "cti", "arrows": "none", "tone": "muted"},
        ],
        "zones": [{"label": "XTM Platform", "row": 2, "col": 1, "row_span": 3, "col_span": 3}],
        "legend": [{"label": "Data flow", "marker": "arrow"}, {"label": "Runs on the core", "marker": "line", "tone": "muted"}],
    }
    block.update(overrides)
    return block


def _architecture_deck(**overrides: object) -> dict:
    return _deck({"layout": "content", "blocks": [_architecture(**overrides)]})


def test_architecture_block_validates_and_keeps_its_graph():
    spec = validate_spec(_architecture_deck(caption="How the modules feed each other"))
    block = spec["slides"][0]["blocks"][0]
    assert block["type"] == "architecture"
    assert [node["id"] for node in block["nodes"]] == ["hub", "cti", "core", "crq", "aev"]
    assert len(block["edges"]) == 5
    # The zone spans 3 x 3 cells around the core with four of them empty: a
    # panel with free corners is the usual shape, and stays allowed.
    assert block["zones"][0]["label"] == "XTM Platform"
    # Defaults the renderer relies on are materialised, not implied.
    assert block["nodes"][0]["col_span"] == 3
    assert block["nodes"][1]["shape"] == "card"
    assert block["edges"][0]["arrows"] == "end"
    assert block["size"] == "md"


def test_architecture_block_is_in_every_catalog():
    assert "architecture" in BLOCK_TYPES
    assert "architecture" in block_catalog()
    reference = spec_reference()
    assert "architecture" in reference
    for field in ("nodes", "edges", "zones", "legend", "logo_file_id", "emphasis", "route", "icon_tile"):
        assert field in reference, field


def test_architecture_node_icon_tile_is_kept_on_a_card_and_a_band():
    """`icon_tile: false` on a card (a band is a card spanning columns) is stored as written, default true."""
    spec = validate_spec(
        _architecture_deck(
            nodes=[
                {"id": "band", "label": "Band", "row": 1, "col": 1, "col_span": 2, "icon": "globe", "icon_tile": False},
                {"id": "ghost", "label": "Ghost", "row": 2, "col": 1, "icon": "shield", "emphasis": "ghost", "icon_tile": False},
                {"id": "card", "label": "Card", "row": 2, "col": 2, "icon": "brain"},
            ],
            edges=[{"source": "band", "target": "card"}, {"source": "ghost", "target": "card"}],
            zones=[],
        )
    )
    nodes = {node["id"]: node for node in spec["slides"][0]["blocks"][0]["nodes"]}
    assert nodes["band"]["icon_tile"] is False
    assert nodes["ghost"]["icon_tile"] is False
    assert nodes["card"]["icon_tile"] is True


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        # Two nodes with the same id: every edge reference would be ambiguous.
        (
            {
                "nodes": [
                    {"id": "a", "label": "A", "row": 1, "col": 1},
                    {"id": "a", "label": "A again", "row": 1, "col": 2},
                ],
                "edges": [],
                "zones": [{"label": "Z", "row": 1, "col": 1, "col_span": 2}],
            },
            "node ids must be unique",
        ),
        # An edge naming a node that does not exist.
        ({"edges": [{"source": "cti", "target": "nowhere"}]}, "names no node"),
        # A self-loop.
        ({"edges": [{"source": "cti", "target": "cti"}]}, "cannot connect to itself"),
        # The same ordered pair twice.
        (
            {"edges": [{"source": "cti", "target": "aev", "label": "x"}, {"source": "cti", "target": "aev", "label": "y"}]},
            "declared twice",
        ),
        # Two nodes on one cell.
        (
            {
                "nodes": [
                    {"id": "a", "label": "A", "row": 1, "col": 1},
                    {"id": "b", "label": "B", "row": 1, "col": 1},
                ],
                "edges": [{"source": "a", "target": "b"}],
                "zones": [],
            },
            "both occupy row 1, col 1",
        ),
        # A span running past the grid limit.
        (
            {
                "nodes": [
                    {"id": "a", "label": "A", "row": 1, "col": 5, "col_span": 3},
                    {"id": "b", "label": "B", "row": 2, "col": 1},
                ],
                "edges": [{"source": "a", "target": "b"}],
                "zones": [],
            },
            "runs past column 6",
        ),
        # A zone larger than the grid the nodes define (4 rows here).
        ({"zones": [{"label": "Too big", "row": 2, "col": 1, "row_span": 4, "col_span": 3}]}, "runs past the grid"),
        # A zone over empty cells only.
        (
            {
                "nodes": [
                    {"id": "a", "label": "A", "row": 1, "col": 1},
                    {"id": "b", "label": "B", "row": 2, "col": 2},
                ],
                "edges": [{"source": "a", "target": "b"}],
                "zones": [{"label": "Empty", "row": 1, "col": 2}],
            },
            "covers no node",
        ),
        # Two zones sharing a cell.
        (
            {
                "zones": [
                    {"label": "Left", "row": 2, "col": 1, "row_span": 3, "col_span": 2},
                    {"label": "Right", "row": 2, "col": 2, "row_span": 3, "col_span": 2},
                ]
            },
            "overlap",
        ),
        # A zone whose border runs through the middle of a spanning band.
        (
            {"zones": [{"label": "Half", "row": 1, "col": 2, "row_span": 2, "col_span": 2}]},
            "cuts through node 'hub'",
        ),
        # Nodes alone are not a diagram.
        ({"edges": [], "zones": []}, "at least one edge or one zone"),
        # One node floating beside the graph: no edge ties it in, no zone groups it.
        (
            {
                "nodes": [
                    {"id": "a", "label": "A", "row": 1, "col": 1},
                    {"id": "b", "label": "B", "row": 1, "col": 2},
                    {"id": "loose", "label": "Loose", "row": 2, "col": 3},
                ],
                "edges": [{"source": "a", "target": "b"}],
                "zones": [],
            },
            "node 'loose' has no edge and sits in no zone",
        ),
        # A circle cannot span columns.
        (
            {
                "nodes": [
                    {"id": "a", "label": "A", "row": 1, "col": 1, "shape": "circle", "col_span": 2},
                    {"id": "b", "label": "B", "row": 2, "col": 1},
                ],
                "edges": [{"source": "a", "target": "b"}],
                "zones": [],
            },
            "circle node cannot span",
        ),
        # Chips need the room a card has.
        (
            {
                "nodes": [
                    {"id": "a", "label": "A", "row": 1, "col": 1, "shape": "pill", "chips": ["x"]},
                    {"id": "b", "label": "B", "row": 2, "col": 1},
                ],
                "edges": [{"source": "a", "target": "b"}],
                "zones": [],
            },
            "chips need a card",
        ),
        # One medium per node: a logo takes the icon's place, so both is a field that changes nothing.
        (
            {
                "nodes": [
                    {"id": "a", "label": "A", "row": 1, "col": 1, "icon": "brain", "logo_file_id": "11111111-1111-4111-8111-111111111111"},
                    {"id": "b", "label": "B", "row": 2, "col": 1},
                ],
                "edges": [{"source": "a", "target": "b"}],
                "zones": [],
            },
            "icon OR a logo_file_id, not both",
        ),
        # `icon_tile: false` draws the icon inline: without an icon there is nothing to draw.
        (
            {
                "nodes": [
                    {"id": "a", "label": "A", "row": 1, "col": 1, "icon_tile": False},
                    {"id": "b", "label": "B", "row": 2, "col": 1},
                ],
                "edges": [{"source": "a", "target": "b"}],
                "zones": [],
            },
            "icon_tile: false needs an icon",
        ),
        # ...and a pill or a circle draws its icon without a tile already.
        (
            {
                "nodes": [
                    {"id": "a", "label": "A", "row": 1, "col": 1, "icon": "brain", "shape": "pill", "icon_tile": False},
                    {"id": "b", "label": "B", "row": 2, "col": 1},
                ],
                "edges": [{"source": "a", "target": "b"}],
                "zones": [],
            },
            "icon_tile applies to a card",
        ),
        # An invented field is refused, not dropped.
        ({"nodes": [{"id": "a", "label": "A", "row": 1, "col": 1, "colour": "red"}, {"id": "b", "label": "B", "row": 2, "col": 1}], "edges": [{"source": "a", "target": "b"}], "zones": []}, "colour"),
    ],
)
def test_architecture_block_refuses_inconsistent_graphs(overrides: dict, fragment: str):
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(_architecture_deck(**overrides))
    assert fragment in str(exc.value)


def test_architecture_block_bounds():
    too_many = [{"id": f"n{i}", "label": f"N{i}", "row": 1 + i // 6, "col": 1 + i % 6} for i in range(13)]
    with pytest.raises(PresentationSpecError):
        validate_spec(_architecture_deck(nodes=too_many, edges=[{"source": "n0", "target": "n1"}], zones=[]))
    with pytest.raises(PresentationSpecError):
        validate_spec(_architecture_deck(nodes=[{"id": "only", "label": "Alone", "row": 1, "col": 1}], edges=[], zones=[]))
    with pytest.raises(PresentationSpecError):
        validate_spec(
            _architecture_deck(
                nodes=[
                    {"id": "a", "label": "A", "row": 1, "col": 1, "chips": ["1", "2", "3", "4", "5"]},
                    {"id": "b", "label": "B", "row": 2, "col": 1},
                ],
                edges=[{"source": "a", "target": "b"}],
                zones=[],
            )
        )
    with pytest.raises(PresentationSpecError):
        validate_spec(
            _architecture_deck(
                nodes=[
                    {"id": "a", "label": "x" * 41, "row": 1, "col": 1},
                    {"id": "b", "label": "B", "row": 2, "col": 1},
                ],
                edges=[{"source": "a", "target": "b"}],
                zones=[],
            )
        )


def test_architecture_logo_is_a_file_reference():
    """``logo_file_id`` rides the generic ``*_file_id`` walk, so it is verified and packaged like every image."""
    from app.services.presentation_export import referenced_file_ids

    file_id = "11111111-1111-4111-8111-111111111111"
    spec = validate_spec(
        _architecture_deck(
            nodes=[
                {"id": "a", "label": "A", "row": 1, "col": 1, "logo_file_id": file_id},
                {"id": "b", "label": "B", "row": 2, "col": 1},
            ],
            edges=[{"source": "a", "target": "b"}],
            zones=[],
        )
    )
    assert file_id in referenced_file_ids(spec)


def test_frontend_architecture_block_draws_through_react_only():
    """The architecture renderer is SVG through React: no Mermaid, no HTML sink, no ``foreignObject``."""
    view = _read("blocks", "Architecture.tsx")
    layout = _read("blocks", "architecture-layout.ts")
    assert "foreignObject" not in view
    assert "mermaid" not in view.lower()
    assert "innerHTML" not in view
    # Every glyph is a real SVG text node the layout audit and the PPTX extractor can see.
    assert "<text" in view
    # The router avoids nodes and crossings instead of drawing straight lines through cards.
    assert "segmentIntersectsRect" in layout and "segmentsCross" in layout
    # Async media hold the export capture until they have settled.
    assert 'data-deck-pending' in view
    # Node ids from the spec never become CSS classes or DOM ids.
    assert "className={node.id" not in view and "className={`deck-arch-${node.id" not in view


def test_frontend_architecture_drawing_is_never_magnified_and_fits_its_room():
    """The stage is the drawing's own box, read back as the layout's room.

    The renderer publishes the drawing's canvas width and its natural ratio;
    the stylesheet sizes the stage from them with the drawing absolutely
    positioned inside (its compacted size never feeds back into the box it is
    measured from) and caps the `<svg>` at the drawn size, so a 3-column
    diagram on a wide slide is drawn at 1:1 - a row as tall as its content,
    not as the column count makes it - and a `fill` figure never overflows
    the body. The layout takes the room and compacts the rows before the
    slide's shrink-to-fit sees anything.
    """
    view = _read("blocks", "Architecture.tsx")
    layout = _read("blocks", "architecture-layout.ts")
    css = _read("presentation.css")
    # The room is measured, then frozen once it keeps moving (#11580).
    assert "useSettledBox(stageRef, settleKey)" in view
    assert "layoutArchitecture(block, { room, assets })" in view
    for variable in ("--deck-architecture-w", "--deck-architecture-ratio", "--deck-architecture-drawn-h"):
        assert variable in view and variable in css, variable
    assert "layout.width / layout.naturalHeight" in view
    stage = css.split(".deck-root .deck-architecture-stage {", 1)[1].split("}", 1)[0]
    assert "aspect-ratio: var(--deck-architecture-ratio" in stage
    assert "width: min(100%, var(--deck-architecture-w" in stage
    assert "max-height: var(--deck-architecture-h" in stage
    svg = css.split(".deck-root .deck-architecture-svg {", 1)[1].split("}", 1)[0]
    assert "position: absolute;" in svg
    assert "max-width: var(--deck-architecture-w" in svg
    assert "max-height: var(--deck-architecture-drawn-h" in svg
    # The fit pass: a floor on the density, the natural height kept for the stage.
    assert "export const DENSITY_MIN" in layout
    assert "naturalHeight" in layout
    # An edge leaves its source for a free corridor when both elbows are blocked, and lands on a side's centre.
    assert '"vhv"' in layout and '"hvh"' in layout
    assert "straightSides" in layout


def test_stored_v1_decks_validate_as_upgraded_v2_decks():
    """A deck written before the retirement still validates - as the
    v2 deck it is upgraded to. No slide keeps a retired layout and no HTML
    survives: the deck has no field to hold it any more."""
    spec = validate_spec(
        _deck(
            {"layout": "title", "title": "Old deck"},
            {"layout": "two_columns", "title": "T", "left": {"bullets": ["a"]}, "right": {"rich_text": "<p>b</p>"}},
            {"layout": "comparison", "title": "C", "left": {"label": "L"}, "right": {"label": "R"}},
            {"layout": "dashboard", "title": "D", "metrics": [{"label": "x", "value": "1"}]},
            {"layout": "generic", "bullets": ["a"]},
        )
    )
    assert [slide["layout"] for slide in spec["slides"]] == ["title", "content", "content", "content", "content"]
    assert not any(slide["layout"] in RETIRED_LAYOUTS for slide in spec["slides"])
    assert "<p>" not in str(spec) and "rich_text" not in str(spec)
    # The words travelled: the two_columns sides became two columns of blocks.
    columns = spec["slides"][1]["columns"]
    assert [[block["type"] for block in column] for column in columns] == [["list"], ["text"]]
    assert columns[1][0]["content"] == "b"


def test_v1_layouts_are_refused_nowhere_and_never_stored():
    """The tool schema enum, the catalog and the validator agree: the four
    retired ids are not layouts. A slide sent with one is not an error (the
    upgrade is the compatibility contract) but the stored spec never wears
    one, whatever the input mixes."""
    for layout in RETIRED_LAYOUTS:
        assert layout not in SLIDE_LAYOUTS
        assert layout not in layout_catalog()
        spec = validate_spec(_deck({"layout": layout, "title": "Old"}))
        assert spec["slides"][0]["layout"] in SLIDE_LAYOUTS


def test_unknown_field_is_addressed_to_its_block():
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(
            _deck(
                {
                    "layout": "content",
                    "blocks": [{"type": "text", "content": "a"}, {"type": "text", "body": "oops"}],
                }
            )
        )
    assert "blocks.1" in str(exc.value)


# ── Legacy blanks + the two readers of an error ─────────────────────


def test_legacy_blank_optional_strings_mean_absent():
    """A v1 deck stored ``subtitle: ""`` for "no subtitle"; v2 requires a
    non-empty value WHEN one is given. Re-theming, duplicating or restoring
    such a deck re-validates the stored spec and used to fail on fields the
    user never wrote. A blank optional string is absent: it validates
    and is dropped from the normalised spec."""
    spec = validate_spec(
        _deck(
            {"layout": "title", "title": "Old", "subtitle": "", "footer": "  ", "notes": ""},
            {"layout": "content", "blocks": [{"type": "callout", "title": "", "content": "x"}]},
            {"layout": "section", "title": "S", "label": "", "subtitle": "   "},
        )
    )
    slides = spec["slides"]
    assert not {"subtitle", "footer", "notes"} & set(slides[0])
    assert "title" not in slides[1]["blocks"][0]
    assert not {"label", "subtitle"} & set(slides[2])
    # Real values are untouched.
    assert slides[2]["title"] == "S"


def test_blank_optionals_on_a_stored_v1_slide_do_not_break_the_upgrade():
    """The same blanks on the RETIRED shapes: a stored v1 slide
    carrying ``rich_text: ""`` / ``image_file_id: ""`` / ``delta: ""`` is
    upgraded as if the field were absent - no empty text block, no image
    without a file, no blank delta on a metric."""
    spec = validate_spec(
        _deck(
            {"layout": "generic", "title": "G", "bullets": ["a"], "rich_text": "", "image_file_id": ""},
            {
                "layout": "two_columns",
                "title": "T",
                "left": {"heading": "", "bullets": ["a"]},
                "right": {"rich_text": "<p>b</p>", "image_file_id": " "},
            },
            {"layout": "dashboard", "title": "D", "metrics": [{"label": "x", "value": "1", "delta": ""}]},
        )
    )
    generic, two_columns, dashboard = spec["slides"]
    assert [block["type"] for block in generic["blocks"]] == ["list"]
    assert [[block["type"] for block in column] for column in two_columns["columns"]] == [["list"], ["text"]]
    (metrics,) = dashboard["blocks"]
    assert metrics["type"] == "metrics_grid"
    assert "delta" not in metrics["items"][0]
    assert "rich_text" not in str(spec) and "image" not in {b["type"] for b in generic["blocks"]}


@pytest.mark.parametrize(
    ("payload", "path"),
    [
        (_deck({"layout": "title", "title": ""}), "slides.0.title"),
        (_deck({"layout": "content", "blocks": [{"type": "text", "content": "   "}]}), "slides.0.blocks.0.content"),
        ({"title": "", "slides": [{"layout": "title", "title": "x"}]}, "title"),
    ],
)
def test_blank_required_strings_are_still_refused(payload, path):
    """The normalisation is scoped to OPTIONAL fields: a required string left
    blank is still the error it always was, addressed to the field."""
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(payload)
    message = str(exc.value)
    assert path in message
    assert "at least 1 character" in message


def test_blank_optional_string_is_still_refused_where_it_carries_meaning():
    """A callout with a blank title and nothing else is empty, not "a callout
    with no title": the after-validator still sees the normalised ``None``."""
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(_deck({"layout": "content", "blocks": [{"type": "callout", "title": ""}]}))
    assert "a callout needs a title, content or children" in str(exc.value)


def _failing_deck() -> dict:
    return _deck(
        {"layout": "title", "title": "<b>html</b>"},
        {"layout": "content", "blocks": [{"type": "list", "items": [{"text": "ok"}, {"text": "**open"}]}]},
        {"layout": "content", "blocks": [{"type": "text", "content": "a", "bogus": 1}]},
    )


def test_agent_and_human_readers_get_the_same_problems_in_their_own_words():
    """``str(exc)`` stays the tool error the agent repairs from (0-based paths
    it built, ``layout=`` / ``block=`` notes, "call the tool again", the
    catalog). ``human_message()`` is what a route a PERSON is behind returns:
    slides numbered as the editor numbers them, the block as an "in <block>"
    note, and no instruction meant for a model."""
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(_failing_deck())
    err = exc.value
    assert len(err.problems) == 3 == err.total

    agent = str(err)
    assert "call the tool again" in agent
    assert "slides.0.title (layout=title)" in agent
    assert "slides.1.blocks.0.items.1.text (layout=content, block=list)" in agent
    assert "slides.2.blocks.0.bogus (layout=content, block=text)" in agent
    assert "Allowed slide layouts:" in agent

    human = err.human_message()
    assert human.startswith("The presentation cannot be saved as it is. Fix the following and try again:\n")
    assert "call the tool again" not in human
    assert "Allowed slide layouts" not in human
    assert "layout=" not in human and "block=" not in human
    assert "\n- Slide 1 (title): title - " in human
    assert "\n- Slide 2 (content): blocks.0.items.1.text in list - " in human
    assert "\n- Slide 3 (content): blocks.0.bogus in text - Extra inputs are not permitted" in human
    assert err.human_details() == human.split(":\n", 1)[1]


def test_human_message_of_a_root_problem_has_no_slide():
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec({"title": "D", "slides": []})
    human = exc.value.human_message()
    assert "\n- Slides - " in human
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec("not a dict")
    # No structured problems: the human message is the plain message.
    assert exc.value.problems == ()
    assert exc.value.human_message() == str(exc.value)


def test_union_tags_are_lifted_only_where_unions_live():
    """Pydantic inserts the chosen union member into an error path. Lifting
    it out must be structural — a FIELD that happens to spell a tag
    (``items.1.text``, ``items.0.title``) is part of the path the agent built
    and the person reads, and was previously swallowed as a block note."""
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(
            _deck(
                # columns: list of lists of blocks -> two indices before the tag.
                {"layout": "content", "columns": [[{"type": "text", "content": ""}]]},
                # footer_block: a single union-typed field -> tag right after it.
                {
                    "layout": "content",
                    "blocks": [{"type": "text", "content": "x"}],
                    "footer_block": {"type": "callout", "title": "", "content": ""},
                },
                # children: nested atomic blocks inside a callout.
                {
                    "layout": "content",
                    "blocks": [
                        {"type": "callout", "content": "x", "children": [{"type": "list", "items": [{"text": ""}]}]}
                    ],
                },
                # numbered_cards items have a literal ``title`` field.
                {
                    "layout": "content",
                    "blocks": [{"type": "numbered_cards", "items": [{"title": "", "description": "x"}]}],
                },
            )
        )
    by_path = {p.path: p for p in exc.value.problems}
    assert by_path["slides.0.columns.0.0.content"].blocks == ("text",)
    assert by_path["slides.1.footer_block"].blocks == ("callout",)
    assert by_path["slides.2.blocks.0.children.0.items.0.text"].blocks == ("callout", "list")
    assert by_path["slides.3.blocks.0.items.0.title"].blocks == ("numbered_cards",)
    assert all(p.layout == "content" for p in exc.value.problems)
    assert "Slide 3 (content): blocks.0.children.0.items.0.text in callout > list" in exc.value.human_message()


# ── Catalog + reference ─────────────────────────────────────────────────────


def test_catalogs_cover_every_layout_and_block():
    assert set(layout_catalog()) == set(SLIDE_LAYOUTS)
    assert set(block_catalog()) == set(BLOCK_TYPES)
    assert set(CONTAINER_BLOCK_TYPES) <= set(BLOCK_TYPES)


def test_spec_reference_names_every_layout_and_block():
    reference = spec_reference()
    for layout in SLIDE_LAYOUTS:
        assert layout in reference
    for block in BLOCK_TYPES:
        assert block in reference
    # The retired v1 layouts are not a thing the agent is told about
    # in any form: nothing to opt into, nothing to avoid. The layout section
    # lists exactly the catalog (`comparison` survives as a BLOCK type).
    layouts_section = reference.split("SLIDE LAYOUTS", 1)[1].split("\n\n", 1)[0]
    listed = {line[2:].split(":", 1)[0] for line in layouts_section.splitlines() if line.startswith("- ")}
    assert listed == set(SLIDE_LAYOUTS)
    for layout in RETIRED_LAYOUTS:
        assert f'"layout": "{layout}"' not in reference
    assert "rich_text" not in reference


# ── Spec: opt-in `fill` on the grid blocks ──────────────────────────
#
# A content slide holding one six-item ``feature_list`` packed the rows under
# the title and left the bottom third bare. ``fill`` is the block's own way of
# spreading its rows over the height the run leaves it - off by default, so a
# deck that never sets it renders exactly as before.

#: One minimal instance of each fill-capable block.
_FILL_BLOCKS: dict[str, dict] = {
    "list": {"type": "list", "items": ["One", "Two", "Three"]},
    "metrics_grid": {"type": "metrics_grid", "items": [{"value": "12", "label": "A"}, {"value": "34", "label": "B"}]},
    "key_value": {"type": "key_value", "items": [{"label": "Tenant", "value": "acme"}]},
    "feature_list": {
        "type": "feature_list",
        "items": [{"icon": "shield-check", "title": "Detect"}, {"icon": "rocket", "title": "Respond"}],
    },
    "numbered_cards": {"type": "numbered_cards", "items": ["First", "Second"]},
    "personas": {"type": "personas", "items": [{"name": "CISO", "tagline": "Owns the risk"}]},
    "table": {"type": "table", "headers": ["Metric", "Q1"], "rows": [["Revenue", "12"], ["Cost", "8"]]},
}

#: The root class each block's renderer emits `data-fill` on (its CSS hook).
_FILL_ROOT_CLASSES: dict[str, str] = {
    "list": "deck-list",
    "metrics_grid": "deck-metrics",
    "key_value": "deck-kv",
    "feature_list": "deck-features",
    "numbered_cards": "deck-cards",
    "personas": "deck-personas",
    "table": "deck-table-block",
}


def test_the_fill_capable_grid_blocks_are_the_documented_seven():
    assert set(FILL_BLOCK_TYPES) == set(_FILL_BLOCKS) == set(_FILL_ROOT_CLASSES)
    assert set(FILL_BLOCK_TYPES) <= set(BLOCK_TYPES)
    for block in FILL_BLOCK_TYPES:
        assert "fill" in block_catalog()[block], block


@pytest.mark.parametrize("block", FILL_BLOCK_TYPES)
def test_fill_is_accepted_and_off_by_default_on_every_grid_block(block: str):
    plain = validate_spec(_deck(_content(dict(_FILL_BLOCKS[block]))))
    filled = validate_spec(_deck(_content({**_FILL_BLOCKS[block], "fill": True})))
    assert plain["slides"][0]["blocks"][0]["fill"] is False
    assert filled["slides"][0]["blocks"][0]["fill"] is True
    # The switch is the ONLY difference the validator writes: a deck saved
    # before the field reads back as one that says `fill: false`.
    plain_block = dict(plain["slides"][0]["blocks"][0])
    filled_block = dict(filled["slides"][0]["blocks"][0])
    plain_block.pop("fill")
    filled_block.pop("fill")
    assert plain_block == filled_block


def test_fill_is_a_switch_not_a_mode():
    # A bool, like every other `fill`: there is no "space-between" / "stretch"
    # to pick - HOW the rows spread is the renderer's, per block.
    with pytest.raises(PresentationSpecError):
        validate_spec(_deck(_content({**_FILL_BLOCKS["feature_list"], "fill": "stretch"})))


def test_the_reference_documents_fill_on_every_grid_block_in_the_wording_its_renderer_keeps():
    """The row-spreading roots say the spread is bounded (``spread.ts``
    caps them at a readable gap); ``numbered_cards`` says its cards share the
    height, because that renderer is the one exception - its cards grow, not
    its gaps - and that the cards stop at a readable height (its cap is
    one extra per row of cards). One wording per behaviour, so the agent is
    never promised a gap the renderer does not keep."""
    lines = {line[2:].split(":", 1)[0]: line for line in spec_reference().splitlines() if line.startswith("- ")}
    for block in FILL_BLOCK_TYPES:
        assert f"fill?: bool = false ({fill_description(block)})" in lines[block], block
    assert fill_description("numbered_cards") == FILL_CARDS_DESCRIPTION != FILL_ROWS_DESCRIPTION
    assert "readable gap" in FILL_ROWS_DESCRIPTION
    assert "share" in FILL_CARDS_DESCRIPTION and "gap" not in FILL_CARDS_DESCRIPTION
    assert "readable card height" in FILL_CARDS_DESCRIPTION
    assert {fill_description(block) for block in FILL_BLOCK_TYPES if block != "numbered_cards"} == {
        FILL_ROWS_DESCRIPTION
    }
    # A lone card is sized to its content: its `fill` only lines it up
    # with the cards beside it, never stretches it to the floor.
    assert f"fill?: bool = false ({CARD_FILL_DESCRIPTION})" in lines["phase_card"]
    assert f"fill?: bool = false ({CARD_FILL_DESCRIPTION})" in lines["callout"]
    assert "never taller than its content alone" in CARD_FILL_DESCRIPTION


def test_frontend_renderers_emit_data_fill_from_the_field_and_the_css_spreads_it():
    """Each grid block's root emits ``data-fill`` from its ``fill`` field and
    its root class has the ``[data-fill="true"]`` rule that spreads the rows;
    the generic ``.deck-blocks > [data-fill="true"]`` hook hands the block the
    free height. A block wired on one side only either never grows or grows
    with its rows still packed at the top."""
    css = _read("presentation.css")
    assert '.deck-root .deck-blocks > [data-fill="true"]' in css
    sources = "\n".join(_read("blocks", name) for name in ("cards.tsx", "list.tsx", "brief.tsx", "metrics.tsx", "table.tsx"))
    for block in FILL_BLOCK_TYPES:
        root = _FILL_ROOT_CLASSES[block]
        # The table's rule is scoped to a run or a dashboard panel body
        # (#10228): a `fill` table spreads its body rows to the panel's floor.
        hook = (
            f'.deck-root :is(.deck-blocks, .deck-panel-body) > .{root}[data-fill="true"]'
            if block == "table"
            else f'.deck-root .{root}[data-fill="true"]'
        )
        assert hook in css, block
        if block == "numbered_cards":
            # Reuses the takeaways slide's `NumberedCards` prop rather than a second wiring.
            assert re.search(r"<NumberedCards[^>]*\bfill=\{block\.fill\}", sources), block
        else:
            assert re.search(
                rf'className="{root}"(?:\s+[^>]*?)?\s+data-fill=\{{block\.fill \? "true" : undefined\}}',
                sources,
                re.S,
            ), block


def test_frontend_types_carry_fill_on_every_grid_block():
    types = (FRONTEND_ROOT / "src" / "types" / "index.ts").read_text(encoding="utf-8")
    interfaces = {
        "list": "DeckListBlock",
        "metrics_grid": "DeckMetricsGridBlock",
        "key_value": "DeckKeyValueBlock",
        "feature_list": "DeckFeatureListBlock",
        "numbered_cards": "DeckNumberedCardsBlock",
        "personas": "DeckPersonasBlock",
        "table": "DeckTableBlock",
    }
    assert set(interfaces) == set(FILL_BLOCK_TYPES)
    for block, name in interfaces.items():
        body = types.split(f"export interface {name} ", 1)[1].split("\n}\n", 1)[0]
        assert "fill?: boolean;" in body, block


# ── Spec: opt-in `distribute` on content slides ─────────────────────
#
# `fill` is one block spreading its rows; `distribute` is the SLIDE spreading
# its blocks - the free height lands between them, the way the agenda spreads
# its rows. Off by default (the agenda's is on), so a deck that never
# sets it packs its run to the top exactly as before.

_DISTRIBUTE_TEXT_BLOCKS = [{"type": "text", "content": f"Paragraph {n}"} for n in (1, 2, 3)]


def _content_columns(**overrides) -> dict:
    slide = {
        "layout": "content",
        "title": "Side by side",
        "columns": [[{"type": "text", "content": "Left"}], [{"type": "text", "content": "Right"}]],
    }
    slide.update(overrides)
    return slide


def test_distribute_is_accepted_and_off_by_default_on_both_content_forms():
    packed = validate_spec(_deck(_content(*_DISTRIBUTE_TEXT_BLOCKS)))
    spread = validate_spec(_deck({**_content(*_DISTRIBUTE_TEXT_BLOCKS), "distribute": True}))
    assert packed["slides"][0]["distribute"] is False
    assert spread["slides"][0]["distribute"] is True
    columns_packed = validate_spec(_deck(_content_columns()))
    columns_spread = validate_spec(_deck(_content_columns(distribute=True)))
    assert columns_packed["slides"][0]["distribute"] is False
    assert columns_spread["slides"][0]["distribute"] is True
    # The switch is the ONLY difference the validator writes (slide ids are
    # minted per validation), and a deck saved before the field reads back as
    # one that says `distribute: false`.
    def without(slide: dict, *keys: str) -> dict:
        return {k: v for k, v in slide.items() if k not in keys}

    for a, b in ((packed, spread), (columns_packed, columns_spread)):
        assert without(a["slides"][0], "id", "distribute") == without(b["slides"][0], "id", "distribute")
    explicit = validate_spec(_deck({**_content(*_DISTRIBUTE_TEXT_BLOCKS), "distribute": False}))
    assert without(explicit["slides"][0], "id") == without(packed["slides"][0], "id")


def test_distribute_is_a_switch_like_the_agendas():
    with pytest.raises(PresentationSpecError):
        validate_spec(_deck({**_content(*_DISTRIBUTE_TEXT_BLOCKS), "distribute": "sometimes"}))
    # Mirrors AgendaSlide in name and meaning; only the default differs (an
    # agenda spreads unless told not to, a content slide packs unless told to).
    from app.services.presentation_spec import AgendaSlide, ContentSlide

    agenda = AgendaSlide.model_fields["distribute"]
    content = ContentSlide.model_fields["distribute"]
    assert agenda.default is True and content.default is False
    assert content.description.startswith(
        "spread the blocks evenly over the body height (free height lands between blocks, "
        "each gap up to a readable maximum); false packs them to the top"
    )
    assert "Wins over vertical_align" in content.description


def test_the_reference_documents_distribute_on_content_slides():
    fields = layout_catalog()["content"]
    assert fields.index("vertical_align") < fields.index("distribute") < fields.index("equal_height")
    content_line = next(line for line in spec_reference().splitlines() if line.startswith("- content:"))
    assert "distribute?: bool = false (spread the blocks evenly over the body height" in content_line
    assert "Wins over vertical_align for the run" in content_line


def test_generate_presentation_tool_documents_distribute_for_content_slides_too():
    """The flat per-slide schema has ONE `distribute` slot: it keeps the agenda
    wording first (pinned elsewhere) and now says what the switch does on a
    content slide, and `vertical_align` says which of the two wins."""
    import uuid

    from app.services.builtin_tools import build_presentation_tools

    defs, _executor = build_presentation_tools(db=None, user_id=str(uuid.uuid4()), conversation_id=None)  # type: ignore[arg-type]
    tool = next(d for d in defs if d.name == "generate_presentation")
    props = tool.input_schema["properties"]["slides"]["items"]["properties"]
    description = props["distribute"]["description"]
    assert description.startswith("agenda:")
    assert "content: spread the blocks evenly over the body height (default false" in description
    assert "wins over vertical_align for the run" in description
    assert "distribute wins over it" in props["vertical_align"]["description"]
    # Columns lined up as rows are not spread: the switch is offered and says so.
    assert "equal_rows wins over it on columns" in description
    assert props["equal_rows"]["type"] == "boolean"
    assert props["equal_rows"]["description"].startswith("content with columns: the Nth block of every column")


def test_frontend_content_layout_emits_data_distribute_and_the_css_spreads_the_run():
    """`ContentLayout` emits `data-distribute` on the body host in both forms,
    and the CSS spreads the run through `justify-content: space-between` -
    written AFTER the `vertical_align` rules at the same specificity, which is
    what makes `distribute` win over them, as the schema says. On a columns
    slide the columns take the body's height first, or there is nothing to
    spread over."""
    types = (FRONTEND_ROOT / "src" / "types" / "index.ts").read_text(encoding="utf-8")
    content_type = types.split("export interface ContentSlide ", 1)[1].split("\n}\n", 1)[0]
    assert "distribute?: boolean;" in content_type
    blocks = _read("blocks", "index.tsx")
    assert 'data-distribute={distribute ? "true" : undefined}' in blocks
    body = _read("layouts", "body.tsx")
    content_layout = body.split("export function ContentLayout(", 1)[1].split("\nexport function ", 1)[0]
    # The columns host spreads unless its columns line their blocks up as rows.
    assert "const spread = slide.distribute && !slide.equal_rows;" in content_layout
    assert 'data-distribute={spread ? "true" : undefined}' in content_layout  # columns host
    assert "distribute={isLoneStatement(slide.blocks) ? undefined : slide.distribute}" in content_layout  # single flow
    css = _read("presentation.css")
    run_rule = '.deck-root .deck-blocks-slide[data-distribute="true"] { justify-content: space-between; }'
    assert run_rule in css
    for valign in ("center", "end"):
        assert css.index(f'.deck-root .deck-blocks-slide[data-valign="{valign}"]') < css.index(run_rule), valign
    columns_rule = '.deck-root .deck-columns-slide[data-distribute="true"] { align-items: stretch; }'
    assert columns_rule in css
    assert (
        '.deck-root .deck-columns-slide[data-distribute="true"] .deck-column > .deck-blocks {\n'
        "  justify-content: space-between;\n}"
    ) in css
    for valign in ("center", "end", "stretch"):
        assert css.index(f'.deck-root .deck-columns[data-valign="{valign}"]') < css.index(columns_rule), valign
    # The export reads the rendered boxes, so a spread run is exported where it
    # was drawn; no second implementation of the rule in the extractor.
    from app.services.presentation_export import EXTRACT_PRIMITIVES_JS

    assert "distribute" not in EXTRACT_PRIMITIVES_JS


# ── Spec: charts and dashboards ─────────────────────────────────────


def _chart(**overrides) -> dict:
    block = {
        "type": "chart",
        "kind": "bar",
        "categories": ["Q1", "Q2", "Q3"],
        "series": [{"name": "ARR", "values": [18.2, 21.4, 24.9]}],
    }
    block.update(overrides)
    return block


def _content(*blocks: dict) -> dict:
    return {"layout": "content", "title": "Figures", "blocks": list(blocks)}


def _dashboard(**overrides) -> dict:
    slide = {
        "layout": "dashboard",
        "title": "Adoption",
        "tiles": [{"value": "6,250", "label": "Instances"}, {"value": "38%", "label": "On 6.x"}],
        "panels": [{"title": "Instances", "span": 2, "block": _chart(kind="line")}, {"block": _chart()}],
    }
    slide.update(overrides)
    return slide


def _refused(deck: dict) -> str:
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(deck)
    return str(exc.value)


@pytest.mark.parametrize(
    "chart",
    [
        _chart(kind="bar", horizontal=True, unit="users"),
        _chart(kind="stacked_bar", series=[{"name": "CE", "values": [1, 2, 3]}, {"name": "EE", "values": [4, 5, 6]}]),
        _chart(kind="line", format="currency", unit="USD", target=30, goal_label="Plan"),
        _chart(kind="area", stacked=True, series=[{"name": "A", "values": [1, 2, 3]}, {"name": "B", "values": [1, 1, 1]}]),
        _chart(kind="pie", categories=["CE", "EE"], series=[{"name": "Share", "values": [64, 36]}], format="percent"),
        _chart(kind="donut", categories=["CE", "EE"], series=[{"name": "Share", "values": [64, 36]}], show_values=False),
        _chart(kind="gauge", categories=["Coverage"], series=[{"name": "MITRE", "values": [86]}], format="percent"),
        _chart(
            kind="combo",
            series=[{"name": "Revenue", "values": [1, 2, 3], "kind": "bar"}, {"name": "Margin", "values": [4, 5, 6], "kind": "line"}],
        ),
        _chart(kind="waterfall", series=[{"name": "ARR", "values": [18.2, 3.2, 21.4]}], totals=[0, 2]),
        _chart(kind="map", categories=["FR", "US", "BR"], series=[{"name": "Customers", "values": [31, 42, 7]}]),
    ],
    ids=lambda chart: chart["kind"],
)
def test_every_chart_kind_validates_in_a_content_body_and_on_a_dashboard_panel(chart):
    spec = validate_spec(_deck(_content(chart), _dashboard(panels=[{"block": chart}])))
    body, panel = spec["slides"][0]["blocks"][0], spec["slides"][1]["panels"][0]["block"]
    assert body["type"] == panel["type"] == "chart" and body["kind"] == chart["kind"]
    assert "chart" in BLOCK_TYPES and "dashboard" in SLIDE_LAYOUTS


def test_a_chart_series_must_have_one_value_per_category_and_the_error_names_the_field():
    """The error is addressed to the slide,
    its layout, the panel and the series - the agent patches ONE field."""
    short = _chart(series=[{"name": "ARR", "values": [1, 2, 3]}, {"name": "Plan", "values": [1]}])
    message = _refused(_deck({"layout": "title", "title": "T"}, _dashboard(panels=[{"block": _chart()}, {"block": short}])))
    assert "slides.1" in message and "layout=dashboard" in message
    assert "panels.1.block" in message and "series.1.values" in message
    assert "1 value(s) but the chart has 3 categories (length mismatch)" in message
    body = _refused(_deck(_content(short)))
    assert "slides.0.blocks.0" in body and "block=chart" in body and "length mismatch" in body


def test_pie_donut_and_gauge_plot_exactly_one_series_and_no_negative_share():
    two = [{"name": "A", "values": [1, 2, 3]}, {"name": "B", "values": [1, 2, 3]}]
    for kind in ("pie", "donut"):
        assert "exactly one series" in _refused(_deck(_content(_chart(kind=kind, series=two))))
        assert "negative share" in _refused(_deck(_content(_chart(kind=kind, series=[{"name": "S", "values": [5, -1, 2]}]))))
    assert "exactly one series" in _refused(
        _deck(_content(_chart(kind="gauge", categories=["x"], series=[{"name": "A", "values": [1]}, {"name": "B", "values": [2]}])))
    )


def test_a_share_chart_has_no_target_to_draw():
    """A target is a reference line on an axis or the maximum of a gauge; a
    pie / donut has neither, so the field is refused instead of silently
    dropped - the renderer would otherwise have nothing to name in the legend."""
    share = [{"name": "Share", "values": [64, 36]}]
    for kind in ("pie", "donut"):
        message = _refused(_deck(_content(_chart(kind=kind, categories=["CE", "EE"], series=share, target=50))))
        assert f"a {kind} has neither" in message and "drop target" in message


# ── Map ───────────────────────────────────────────────────────────────


def _map(**overrides) -> dict:
    """The issue's shape - four countries, one series, values on - with fictitious figures."""
    fields = {
        "kind": "map",
        "categories": ["FR", "US", "AU", "GB"],
        "series": [{"name": "Customers", "values": [84, 36, 14, 9]}],
        "show_values": True,
    }
    fields.update(overrides)
    return _chart(**fields)


def test_a_map_reads_country_codes_on_a_content_slide_and_in_a_dashboard_panel():
    spec = validate_spec(_deck(_content(_map(title="Customers by country")), _dashboard(panels=[{"block": _map()}])))
    body, panel = spec["slides"][0]["blocks"][0], spec["slides"][1]["panels"][0]["block"]
    for block in (body, panel):
        assert block["kind"] == "map" and block["categories"] == ["FR", "US", "AU", "GB"]
        # Never materialised: a map that says nothing is the world.
        assert "region" not in block
    assert body["title"] == "Customers by country"


def test_a_map_refuses_every_code_iso_3166_1_does_not_assign_naming_its_index():
    """A user-assigned code (XK), an exceptionally reserved one (UK, EU), a
    name and a three-letter code are refused together, each by its index, and
    the refusal is addressed to the block."""
    four = [{"name": "Customers", "values": [1, 2, 3, 4]}]
    message = _refused(_deck(_content(_map(categories=["FR", "UK", "US", "EU"], series=four))))
    assert "slides.0.blocks.0" in message and "block=chart" in message
    assert 'categories.1 "UK", categories.3 "EU" are not ISO 3166-1 alpha-2 country codes' in message
    assert "two-letter country codes (FR, US, GB, AU)" in message
    for code in ("XK", "France", "FRA", "F"):
        one = _refused(_deck(_content(_map(categories=[code], series=[{"name": "C", "values": [1]}]))))
        assert f'categories.0 "{code}" is not an ISO 3166-1 alpha-2 country code' in one


def test_a_map_stores_its_codes_upper_case_and_takes_each_country_once():
    spec = validate_spec(_deck(_content(_map(categories=["fr", "Us", "au", "GB"]))))
    assert spec["slides"][0]["blocks"][0]["categories"] == ["FR", "US", "AU", "GB"]
    message = _refused(_deck(_content(_map(categories=["FR", "US", "fr", "GB"]))))
    assert 'categories.2 "FR" repeats categories.0' in message and "one value per country" in message


def test_a_map_is_one_series_with_no_axis_no_target_and_no_stack():
    two = [{"name": "A", "values": [1, 2, 3, 4]}, {"name": "B", "values": [1, 2, 3, 4]}]
    assert "a map colours each country by one series, not 2" in _refused(_deck(_content(_map(series=two))))
    assert "a map has neither; drop target" in _refused(_deck(_content(_map(target=50))))
    assert "y_max pins a value axis, which a map chart does not have" in _refused(_deck(_content(_map(y_max=100))))
    assert "stacked only applies to bar / area charts, not map" in _refused(_deck(_content(_map(stacked=True))))
    assert "horizontal only applies to bar / stacked_bar charts, not map" in _refused(_deck(_content(_map(horizontal=True))))
    own = [{"name": "Customers", "values": [84, 36, 14, 9], "show_values": False}]
    assert "series.0.show_values only applies" in _refused(_deck(_content(_map(series=own))))
    axis = [{"name": "Customers", "values": [84, 36, 14, 9], "axis": "secondary"}]
    assert "series.0.axis only applies to a bar, line or combo chart" in _refused(_deck(_content(_map(series=axis))))


def test_a_map_takes_a_value_per_country_past_an_axis_charts_sixteen_categories():
    every = sorted(ISO_3166_ALPHA2)
    spec = validate_spec(_deck(_content(_map(categories=every, series=[{"name": "C", "values": list(range(len(every)))}]))))
    assert len(spec["slides"][0]["blocks"][0]["categories"]) == MAX_MAP_CATEGORIES == 249
    seventeen = [f"M{month}" for month in range(17)]
    message = _refused(_deck(_content(_chart(categories=seventeen, series=[{"name": "C", "values": list(range(17))}]))))
    assert f"a bar chart takes at most {MAX_CHART_CATEGORIES} categories, not 17" in message
    assert f"up to {MAX_MAP_CATEGORIES}" in message


def test_a_maps_region_zooms_it_and_any_other_kind_drops_it():
    for region in MAP_REGIONS:
        spec = validate_spec(_deck(_content(_map(region=region))))
        assert spec["slides"][0]["blocks"][0]["region"] == region
    assert "region" in _refused(_deck(_content(_map(region="asia"))))
    # A map switched to a bar chart in the inspector keeps its zoom out of the save.
    spec = validate_spec(_deck(_content(_chart(region="europe"))))
    assert "region" not in spec["slides"][0]["blocks"][0]
    reference = spec_reference()
    assert '- MapRegion: "world"|"europe"|"americas"|"apac"' in reference
    assert "region?: MapRegion (map only: the part of the world drawn" in reference
    assert "a map's are ISO 3166-1 alpha-2 country codes (FR, US, GB), one per country" in reference


#: The issue's classed map: a flat choropleth, no values, no markers.
_CLASSED = {
    "categories": ["FR", "US", "AU", "GB", "SA", "SG", "BE"],
    "series": [{"name": "Employees", "values": [120, 40, 12, 12, 7, 6, 1]}],
    "show_values": False,
    "legend_style": "classes",
    "markers": False,
}


def test_a_classed_map_without_markers_saves_on_a_content_slide_and_in_a_dashboard_panel():
    spec = validate_spec(_deck(_content(_map(**_CLASSED)), _dashboard(panels=[{"block": _map(**_CLASSED)}])))
    body, panel = spec["slides"][0]["blocks"][0], spec["slides"][1]["panels"][0]["block"]
    for block in (body, panel):
        assert block["legend_style"] == "classes" and block["markers"] is False and block["show_values"] is False
        # Never materialised: a classed map that names no count draws up to five.
        assert "classes" not in block
    forced = validate_spec(_deck(_content(_map(**_CLASSED, classes=3))))
    assert forced["slides"][0]["blocks"][0]["classes"] == 3


def test_a_maps_class_count_is_two_to_seven():
    for count in (1, 8, 2.5, True):
        message = _refused(_deck(_content(_map(**_CLASSED, classes=count))))
        assert "slides.0.blocks.0.classes" in message and "Input should be 2, 3, 4, 5, 6 or 7" in message, count


def test_a_ramp_legend_is_stored_as_unset_and_drops_its_class_count():
    """``ramp`` is what an unset map legend draws: a map that names it saves as
    one that does not, so it renders as today. A class count on a ramp counts
    nothing and is dropped, so switching the legend back to the ramp in the
    inspector still saves. ``markers`` is stored as given, like ``symmetric``."""
    spec = validate_spec(_deck(_content(_map(legend_style="ramp", classes=4))))
    block = spec["slides"][0]["blocks"][0]
    assert not {"legend_style", "markers", "classes"} & block.keys()
    assert block == validate_spec(_deck(_content(_map())))["slides"][0]["blocks"][0]
    marked = validate_spec(_deck(_content(_map(markers=True))))["slides"][0]["blocks"][0]
    assert marked["markers"] is True


def test_a_map_switched_to_another_kind_drops_its_legend_style_class_count_and_markers():
    spec = validate_spec(_deck(_content(_chart(legend_style="classes", classes=3, markers=False))))
    assert not {"legend_style", "markers", "classes"} & spec["slides"][0]["blocks"][0].keys()
    assert "legend_style" in _refused(_deck(_content(_map(legend_style="steps"))))
    reference = spec_reference()
    assert '- MapLegendStyle: "ramp"|"classes"' in reference
    assert "legend_style?: MapLegendStyle (map only: omit for ramp" in reference
    assert "classes?: 2|3|4|5|6|7 (map with legend_style classes: how many classes" in reference
    assert "markers?: bool (map only: false draws no dot over a country too small to see" in reference
    assert "a classed map's right" in reference


def test_the_class_counts_and_legend_styles_are_the_ones_the_map_draws():
    """The validator's class counts and default are the drawing's
    (``chart-format.ts``) and the inspector's (``block-schema.ts``)."""
    drawing = _read("blocks", "chart-format.ts")
    bounds = {name: int(value) for name, value in re.findall(r"export const (MAP_\w+_CLASSES) = (\d+);", drawing)}
    counts = get_args(MapClassCount)
    assert bounds == {"MAP_DEFAULT_CLASSES": MAP_DEFAULT_CLASSES, "MAP_MIN_CLASSES": min(counts), "MAP_MAX_CLASSES": max(counts)}
    assert list(counts) == list(range(min(counts), max(counts) + 1))
    schema = _read("block-schema.ts")
    assert f"export const MAP_CLASS_COUNTS = [{', '.join(map(str, counts))}] as const;" in schema
    styles = ", ".join(f'"{style}"' for style in MAP_LEGEND_STYLES)
    assert f"export const MAP_LEGEND_STYLES = [{styles}] as const;" in schema


def test_the_country_codes_are_the_ones_the_map_draws():
    """``ISO_3166_ALPHA2`` (the validator's) and ``ISO_3166_NUMERIC`` (the
    drawing's, keyed to the atlas) hold the same 249 codes."""
    source = _read("blocks", "chart-map-countries.ts")
    table = re.search(r"export const ISO_3166_NUMERIC\b[^=]*= \{([^}]*)\}", source)
    assert table, "ISO_3166_NUMERIC is not declared in chart-map-countries.ts"
    drawn = dict(re.findall(r"\b([A-Z]{2}): \"(\d{3})\"", table.group(1)))
    assert set(drawn) == ISO_3166_ALPHA2
    assert len(ISO_3166_ALPHA2) == 249 and not {"XK", "UK", "EU"} & ISO_3166_ALPHA2
    assert len(set(drawn.values())) == len(drawn)


def test_a_gauge_reads_one_value_against_a_maximum():
    one = [{"name": "Coverage", "values": [86]}]
    # No target and not a percentage: the dial has no maximum to fill up to.
    assert "needs its maximum" in _refused(_deck(_content(_chart(kind="gauge", categories=["Coverage"], series=one))))
    # A percent gauge is a 0-100 dial: the target is filled in.
    spec = validate_spec(_deck(_content(_chart(kind="gauge", categories=["Coverage"], series=one, format="percent"))))
    assert spec["slides"][0]["blocks"][0]["target"] == 100
    explicit = validate_spec(_deck(_content(_chart(kind="gauge", categories=["ARR"], series=one, target=120, unit="EUR"))))
    assert explicit["slides"][0]["blocks"][0]["target"] == 120
    assert "greater than zero" in _refused(_deck(_content(_chart(kind="gauge", categories=["x"], series=one, target=0))))
    assert "one category and one value" in _refused(
        _deck(_content(_chart(kind="gauge", categories=["a", "b"], series=[{"name": "S", "values": [1, 2]}], target=5)))
    )


def test_a_combo_mixes_bars_and_lines_and_series_kind_belongs_to_it_alone():
    bars_only = [{"name": "A", "values": [1, 2, 3], "kind": "bar"}, {"name": "B", "values": [1, 2, 3], "kind": "bar"}]
    assert "every series here is a bar" in _refused(_deck(_content(_chart(kind="combo", series=bars_only))))
    unsaid = [{"name": "A", "values": [1, 2, 3], "kind": "bar"}, {"name": "B", "values": [1, 2, 3]}]
    assert "set kind bar or line on each series" in _refused(_deck(_content(_chart(kind="combo", series=unsaid))))
    stray = [{"name": "A", "values": [1, 2, 3], "kind": "line"}]
    assert "series.0.kind only applies to a combo chart" in _refused(_deck(_content(_chart(kind="bar", series=stray))))


def _two_units(**overrides) -> dict:
    """The board's "New logos and ASP" figure: a count against the
    left-hand axis, a price against the right-hand one."""
    block = _chart(
        kind="combo",
        series=[
            {"name": "New logos", "kind": "bar", "values": [24, 42, 56]},
            {"name": "ASP", "kind": "line", "axis": "secondary", "values": [240000, 370000, 540000]},
        ],
        secondary_format="currency",
        secondary_unit="EUR",
    )
    block.update(overrides)
    return block


@pytest.mark.parametrize(
    "chart",
    [
        _two_units(),
        # "Burn and net new ARR": millions in bars, the burn multiple as a line, its 1x reference line.
        _chart(
            kind="combo",
            format="currency",
            series=[
                {"name": "Burn", "kind": "bar", "values": [3.4e6, 3.9e6, 4.6e6]},
                {"name": "Net new ARR", "kind": "bar", "values": [2.1e6, 3.4e6, 4.9e6]},
                {"name": "Burn multiple", "kind": "line", "axis": "secondary", "values": [1.62, 1.15, 0.94]},
            ],
            secondary_format="number",
            secondary_unit="x",
            secondary_target=1,
        ),
        _chart(
            kind="line",
            format="percent",
            series=[{"name": "NRR", "values": [104, 108, 112]}, {"name": "Logos", "values": [610, 640, 690], "axis": "secondary"}],
            secondary_format="number",
        ),
        _chart(
            kind="bar",
            series=[{"name": "Deals", "values": [31, 44, 52], "axis": "primary"}, {"name": "Pipeline", "values": [8e6, 9e6, 12e6], "axis": "secondary"}],
            secondary_format="currency",
        ),
    ],
    ids=["logos-and-asp", "burn-and-net-new-arr", "line", "bar"],
)
def test_a_bar_line_or_combo_chart_reads_a_second_unit_against_a_right_hand_axis(chart):
    spec = validate_spec(_deck(_content(chart), _dashboard(panels=[{"block": chart}])))
    body, panel = spec["slides"][0]["blocks"][0], spec["slides"][1]["panels"][0]["block"]
    assert body == panel
    assert [series.get("axis") for series in body["series"]] == [series.get("axis") for series in chart["series"]]
    for field in ("secondary_format", "secondary_unit", "secondary_target"):
        assert body.get(field) == chart.get(field), field


def test_a_series_reads_against_the_left_hand_axis_unless_it_says_otherwise():
    """``axis`` defaults to primary without being written, like ``kind``: a
    stored chart that never named an axis keeps its shape."""
    stored = validate_spec(_deck(_content(_two_units())))["slides"][0]["blocks"][0]
    assert "axis" not in stored["series"][0] and stored["series"][1]["axis"] == "secondary"
    plain = validate_spec(_deck(_content(_chart())))["slides"][0]["blocks"][0]
    assert all("axis" not in series for series in plain["series"])
    assert not {"secondary_format", "secondary_unit", "secondary_target"} & set(plain)


def test_a_right_hand_axis_says_how_it_reads_and_the_error_names_the_field():
    """A series on the secondary axis of a
    chart that does not say how that axis reads is refused, addressed to the
    slide, the panel and the series - never drawn in the left-hand format."""
    unsaid = _two_units(secondary_format=None, secondary_unit=None)
    message = _refused(_deck({"layout": "title", "title": "T"}, _dashboard(panels=[{"block": _chart()}, {"block": unsaid}])))
    assert "slides.1" in message and "layout=dashboard" in message and "panels.1.block" in message
    assert "series.1.axis secondary needs secondary_format or reads with format" in message
    # The unit alone does not say it either.
    assert "needs secondary_format" in _refused(_deck(_content(_two_units(secondary_format=None))))


def test_a_secondary_series_needs_a_primary_one_beside_it():
    both = [{**series, "axis": "secondary"} for series in _two_units()["series"]]
    message = _refused(_deck(_content(_two_units(series=both))))
    assert "series.0.axis secondary needs a primary series beside it" in message


@pytest.mark.parametrize("kind", ["stacked_bar", "area", "pie", "donut", "gauge"])
def test_an_axis_belongs_to_bar_line_and_combo_charts_alone(kind):
    series = [{"name": "A", "values": [1], "axis": "primary"}]
    chart = _chart(kind=kind, categories=["Q1"], series=series, target=5 if kind == "gauge" else None)
    message = _refused(_deck(_content({key: value for key, value in chart.items() if value is not None})))
    assert "series.0.axis only applies to a bar, line or combo chart" in message


def test_a_secondary_series_joins_no_stack_and_no_horizontal_chart():
    pair = [{"name": "A", "values": [1, 2, 3]}, {"name": "B", "values": [4, 5, 6], "axis": "secondary"}]
    stacked = _chart(series=pair, stacked=True, secondary_format="number")
    assert "series.1.axis secondary cannot join a stack" in _refused(_deck(_content(stacked)))
    horizontal = _chart(series=pair, horizontal=True, secondary_format="number")
    assert "a horizontal chart does not have" in _refused(_deck(_content(horizontal)))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("secondary_format", "percent"),
        ("secondary_unit", "x"),
        ("secondary_decimals", 2),
        ("secondary_unit_scale", "M"),
        ("secondary_target", 1),
    ],
)
def test_the_right_hand_axis_fields_need_a_series_on_that_axis(field, value):
    message = _refused(_deck(_content(_chart(**{field: value}))))
    assert f"{field} describes the right-hand axis" in message and f"drop {field}" in message


def test_the_right_hand_axis_fields_are_bounded_like_their_left_hand_twins():
    assert "secondary_unit" in _refused(_deck(_content(_two_units(secondary_unit="a unit far too long"))))
    assert "secondary_target" in _refused(_deck(_content(_two_units(secondary_target=float("nan")))))
    assert "secondary_format" in _refused(_deck(_content(_two_units(secondary_format="ratio"))))
    assert "axis" in _refused(_deck(_content(_chart(series=[{"name": "S", "values": [1, 2, 3], "axis": "right"}]))))


@pytest.mark.parametrize("label_size", ["sm", "md", "lg"])
def test_a_chart_sets_how_large_its_values_and_axis_text_read(label_size):
    """``label_size`` is the chart's text scale - ``sm`` as on a
    dashboard card - on every kind, in a body and in a dashboard panel."""
    for kind in ("bar", "line", "pie"):
        chart = _chart(kind=kind, label_size=label_size)
        spec = validate_spec(_deck(_content(chart), _dashboard(panels=[{"block": chart}])))
        assert spec["slides"][0]["blocks"][0]["label_size"] == label_size
        assert spec["slides"][1]["panels"][0]["block"]["label_size"] == label_size


def test_a_chart_that_names_no_label_size_reads_at_md_and_an_unknown_one_is_refused():
    assert ChartBlock.model_validate(_chart()).label_size == "md"
    message = _refused(_deck(_content(_chart(label_size="xl"))))
    assert "label_size" in message
    assert "label_size?: Size" in spec_reference()


def test_a_charts_size_names_the_dial_diameters_the_gauge_is_drawn_at():
    """A gauge's ``size`` is its dial's diameter, in a dashboard panel too, and
    ``fill`` the largest dial its canvas holds: the tool description quotes the
    diameters the drawing (``chart-gauge.ts``) reads, one entry per size."""
    source = _read("blocks", "chart-gauge.ts")
    declared = re.search(r"export const GAUGE_DIAMETERS\b[^=]*= \{([^}]*)\}", source)
    assert declared, "GAUGE_DIAMETERS is not declared in chart-gauge.ts"
    entries = {name: int(px) for name, px in re.findall(r"(\w+): (\d+)", declared.group(1))}
    assert entries == GAUGE_DIAMETERS
    assert set(GAUGE_DIAMETERS) == set(get_args(Size))
    chart = next(line for line in spec_reference().splitlines() if line.startswith("- chart:"))
    assert f'size?: MediaSize = "md" ({CHART_SIZE_DESCRIPTION})' in chart
    assert "a gauge's dial diameter, sm 240 / md 360 / lg 480 px, centred on its slide or panel" in chart
    assert f"fill?: bool = false ({CHART_FILL_DESCRIPTION})" in chart
    # A deck saved before says nothing about either, and draws at md, unfilled.
    gauge = ChartBlock.model_validate(
        _chart(kind="gauge", target=10, categories=["Coverage"], series=[{"name": "C", "values": [8]}])
    )
    assert (gauge.size, gauge.fill) == ("md", False)


# ── Where the legend sits ────────────────────────────────────────────


@pytest.mark.parametrize("position", ["top", "bottom", "right"])
def test_a_chart_says_where_its_legend_sits_in_a_body_and_in_a_panel(position):
    """``legend_position`` is kept as written on every kind, in a body and in
    a dashboard panel; it is a placement, so a chart that draws no legend (a
    gauge, ``show_legend`` off) carries it without a word."""
    for kind in ("stacked_bar", "line", "donut"):
        chart = _chart(kind=kind, legend_position=position)
        spec = validate_spec(_deck(_content(chart), _dashboard(panels=[{"block": chart}])))
        assert spec["slides"][0]["blocks"][0]["legend_position"] == position
        assert spec["slides"][1]["panels"][0]["block"]["legend_position"] == position
    silent = _chart(kind="gauge", categories=["NRR"], series=[{"name": "NRR", "values": [86]}], format="percent")
    assert ChartBlock.model_validate({**silent, "legend_position": position}).legend_position == position
    assert ChartBlock.model_validate(_chart(show_legend=False, legend_position=position)).legend_position == position


def test_a_chart_that_names_no_legend_position_stores_none_and_an_unknown_one_is_refused():
    """Unset is today's placement and is never materialised, so every deck
    saved before the field is byte-identical; a side the renderer cannot draw
    is refused by name, and the reference offers the three it can."""
    assert ChartBlock.model_validate(_chart()).legend_position is None
    assert "legend_position" not in validate_spec(_deck(_content(_chart())))["slides"][0]["blocks"][0]
    assert "legend_position" in _refused(_deck(_content(_chart(legend_position="left"))))
    assert 'legend_position?: "top"|"bottom"|"right" (where the legend sits; omit for top' in spec_reference()


# ── How the values past the bars are fitted ──────────────────────────


@pytest.mark.parametrize("fit", ["auto", "stagger", "upright"])
def test_a_chart_says_how_its_values_past_the_bars_are_fitted_in_a_body_and_in_a_panel(fit):
    """``value_label_fit`` is kept as written on the kinds it acts on, in a
    body and in a dashboard panel - the reported combo included - and on the
    others too: it changes nothing there, as the inspector says, and an
    author switching a bar chart to a line keeps it for the way back."""
    for chart in (_chart(kind="bar"), _chart(kind="stacked_bar"), _two_units(), _chart(kind="line")):
        block = {**chart, "value_label_fit": fit}
        spec = validate_spec(_deck(_content(block), _dashboard(panels=[{"block": block}])))
        assert spec["slides"][0]["blocks"][0]["value_label_fit"] == fit
        assert spec["slides"][1]["panels"][0]["block"]["value_label_fit"] == fit


def test_a_chart_that_names_no_value_label_fit_stores_none_and_an_unknown_one_is_refused():
    """Unset is today's rule (auto) and is never materialised, so every deck
    saved before the field is byte-identical; a fit the renderer does not know
    is refused by name, and the reference offers the three it does."""
    assert ChartBlock.model_validate(_chart()).value_label_fit is None
    assert "value_label_fit" not in validate_spec(_deck(_content(_chart())))["slides"][0]["blocks"][0]
    message = _refused(_deck(_content(_chart(value_label_fit="rotate"))))
    assert "value_label_fit" in message
    assert 'value_label_fit?: "auto"|"stagger"|"upright" (bar / stacked_bar / combo, upright or horizontal:' in spec_reference()
    # A horizontal chart is fitted too, and the reference says how.
    assert "on horizontal bars a value in the way of another moved along its row" in spec_reference()


# ── The size the values are tried at first ───────────────────────────


@pytest.mark.parametrize("size", ["sm", "md", "lg", 10, 20, 21, 32])
def test_a_chart_sizes_its_values_apart_from_its_axis_text_in_a_body_and_in_a_panel(size):
    """``value_label_size`` - a text scale or a whole number of canvas px - is
    kept as written beside ``label_size`` on the axis charts, in a body and in
    a dashboard panel, and on a pie too: it changes nothing there, as the
    inspector says, and an author switching kinds keeps it for the way back."""
    pie = _chart(kind="pie", categories=["CE", "EE"], series=[{"name": "Share", "values": [64, 36]}])
    bridge = _chart(kind="waterfall", totals=[0, 2], series=[{"name": "ARR", "values": [10, 2, 12]}])
    for chart in (_chart(kind="bar"), _chart(kind="line"), _two_units(), bridge, pie):
        block = {**chart, "value_label_size": size}
        spec = validate_spec(_deck(_content(block), _dashboard(panels=[{"block": block}])))
        stored = spec["slides"][0]["blocks"][0]
        assert stored["value_label_size"] == size and type(stored["value_label_size"]) is type(size)
        assert stored["label_size"] == "md"
        assert spec["slides"][1]["panels"][0]["block"]["value_label_size"] == size


def test_a_chart_that_names_no_value_label_size_stores_none_and_one_outside_the_domain_is_refused_by_name():
    """Unset is today's figure (the values follow ``label_size``) and is never
    materialised, so every deck saved before the field is byte-identical; a
    size the renderer has no ladder for is refused once, naming the field and
    its domain - a fraction, a numeral in a string or a bool included - and
    the reference documents the field."""
    assert ChartBlock.model_validate(_chart()).value_label_size is None
    assert "value_label_size" not in validate_spec(_deck(_content(_chart())))["slides"][0]["blocks"][0]
    for refused in ("xl", 9, 33, 0, 20.5, 20.0, "20", True, [20]):
        message = _refused(_deck(_content(_chart(value_label_size=refused))))
        assert "value_label_size takes sm, md or lg, or a whole number of canvas px from 10 to 32" in message, refused
        assert message.count("value_label_size takes") == 1, refused
    reference = spec_reference()
    assert "value_label_size?: Size|int (bar / stacked_bar / line / area / combo / waterfall / butterfly:" in reference
    assert "the axis text staying at label_size" in reference
    assert "canvas px 10-32" in reference


# ── Pinned value axes and a series' own values ───────────────────────


def test_a_chart_pins_its_value_axes_and_stores_only_what_it_sets():
    """``y_min`` / ``y_max`` and the ``secondary_`` pair part a line from the
    bars on purpose, in a body and in a dashboard panel; a chart that sets
    none stores none, so every deck saved before them keeps its shape."""
    chart = _two_units(y_max=60, secondary_y_min=-100_000, secondary_y_max=600_000)
    spec = validate_spec(_deck(_content(chart), _dashboard(panels=[{"block": chart}])))
    for block in (spec["slides"][0]["blocks"][0], spec["slides"][1]["panels"][0]["block"]):
        assert (block["y_max"], block["secondary_y_min"], block["secondary_y_max"]) == (60, -100_000, 600_000)
        assert "y_min" not in block
    plain = validate_spec(_deck(_content(_chart())))["slides"][0]["blocks"][0]
    assert not {"y_min", "y_max", "secondary_y_min", "secondary_y_max"} & set(plain)
    assert all("show_values" not in series for series in plain["series"])
    # A horizontal chart pins the one value axis it has, along the bottom.
    horizontal = validate_spec(_deck(_content(_chart(horizontal=True, y_max=30))))["slides"][0]["blocks"][0]
    assert horizontal["y_max"] == 30
    # A bridge pins its axis like any bar chart.
    bridge = _chart(
        kind="waterfall",
        categories=["Open", "Up", "Down", "Close"],
        series=[{"name": "ARR", "values": [10, 5, -3, 12]}],
        totals=[0, 3],
        y_max=20,
    )
    assert validate_spec(_deck(_content(bridge)))["slides"][0]["blocks"][0]["y_max"] == 20


@pytest.mark.parametrize(
    ("chart", "says"),
    [
        (
            _chart(y_max=20),
            "y_max 20 is under series.0.values.2 (24.9): the value axis keeps zero and every value it plots - "
            "set y_max to 24.9 or more, or drop it.",
        ),
        (
            _chart(y_min=5),
            "y_min 5 is above zero (0): the value axis keeps zero and every value it plots - set y_min to 0 or less",
        ),
        (_chart(y_min=-10, y_max=-10), "y_min -10 must be under y_max -10."),
        (_two_units(y_max=20), "y_max 20 is under series.0.values.2 (56): the left-hand axis keeps zero"),
        (_two_units(secondary_y_max=200_000), "secondary_y_max 200,000 is under series.1.values.2 (540,000): the right-hand"),
        (_chart(target=30, y_max=26), "y_max 26 is under target (30)"),
        (
            _chart(kind="stacked_bar", series=[{"name": "A", "values": [1, 2, 3]}, {"name": "B", "values": [4, 5, 6]}], y_max=8),
            "y_max 8 is under the stack of categories.2 (9)",
        ),
        (
            _chart(
                kind="waterfall",
                categories=["Open", "Up", "Down", "Close"],
                series=[{"name": "ARR", "values": [10, 5, -3, 12]}],
                totals=[0, 3],
                y_max=14,
            ),
            "y_max 14 is under the bridge level at categories.1 (15)",
        ),
        (_chart(secondary_y_min=-1), "secondary_y_min describes the right-hand axis"),
        (
            _chart(kind="pie", categories=["CE", "EE"], series=[{"name": "Share", "values": [64, 36]}], y_max=100),
            "y_max pins a value axis, which a pie chart does not have - drop y_max.",
        ),
    ],
)
def test_a_pinned_range_that_would_cut_off_a_value_is_refused_and_names_it(chart, says):
    assert says in _refused(_deck(_content(chart)))


def test_a_pinned_range_is_addressed_to_the_panel_it_stands_in():
    message = _refused(_deck({"layout": "title", "title": "T"}, _dashboard(panels=[{"block": _chart(y_max=20)}])))
    assert "slides.1" in message and "panels.0.block" in message and "y_max 20 is under" in message


@pytest.mark.parametrize("kind", ["bar", "line", "area", "combo"])
def test_a_series_leaves_its_values_to_the_tooltip_on_an_unstacked_chart(kind):
    series = [
        {"name": "Revenue", "values": [1, 2, 3], **({"kind": "bar"} if kind == "combo" else {}), "show_values": False},
        {"name": "Margin", "values": [4, 5, 6], **({"kind": "line"} if kind == "combo" else {})},
    ]
    stored = validate_spec(_deck(_content(_chart(kind=kind, series=series))))["slides"][0]["blocks"][0]
    assert stored["series"][0]["show_values"] is False
    assert "show_values" not in stored["series"][1]


@pytest.mark.parametrize(
    "shape",
    [{"kind": "stacked_bar"}, {"kind": "stacked_bar", "horizontal": True}, {"kind": "bar", "stacked": True}],
    ids=["stacked_bar", "horizontal", "bar_stacked"],
)
def test_a_series_leaves_its_segments_values_to_the_tooltip_on_a_stacked_bar(shape):
    """A stacked bar writes each segment's value (inside it, or past
    the stack when it is too thin), so a series may leave its own to the
    tooltip - the remedy the layout check names for a thin series. Its
    stack's totals still follow the chart."""
    series = [
        {"name": "Enterprise", "values": [12, 14, 16]},
        {"name": "Partners", "values": [0.2, 0.3, 0.4], "show_values": False},
    ]
    stored = validate_spec(_deck(_content(_chart(series=series, **shape))))["slides"][0]["blocks"][0]
    assert stored["series"][1]["show_values"] is False
    assert "show_values" not in stored["series"][0]


@pytest.mark.parametrize(
    ("chart", "shape"),
    [
        (
            _chart(kind="area", stacked=True, series=[{"name": "A", "values": [1, 2, 3], "show_values": True}]),
            "stacked area",
        ),
        (
            _chart(
                kind="waterfall",
                categories=["Open", "Up", "Close"],
                series=[{"name": "ARR", "values": [10, 5, 15], "show_values": False}],
                totals=[0, 2],
            ),
            "waterfall",
        ),
        (_chart(kind="pie", categories=["CE", "EE"], series=[{"name": "Share", "values": [64, 36], "show_values": False}]), "pie"),
    ],
)
def test_a_series_own_show_values_is_refused_where_the_chart_writes_its_values_as_one(chart, shape):
    message = _refused(_deck(_content(chart)))
    assert "series.0.show_values only applies to a bar, stacked bar, line, unstacked area or combo chart" in message
    assert f"a {shape} writes its values as one" in message


def test_the_reference_names_the_pinned_ranges_and_a_series_own_values():
    reference = spec_reference()
    for field in ("y_min?: number", "y_max?: number", "secondary_y_min?: number", "secondary_y_max?: number"):
        assert field in reference
    series = reference[reference.index("- ChartSeries:") :].split("\n", 1)[0]
    assert "show_values?: bool" in series


#: The burn multiple of slide 3 of the Q3 2026 quarterly review.
BURN_MULTIPLE = [0.96, 1.04, 0.47, 1.57, 1.18, 1.56, 1.27]


def _burn_and_net_new_arr(**overrides) -> dict:
    """Slide 3 of the Q3 2026 quarterly review: net new ARR and burn as
    bars in euros, the burn multiple as a line against the right-hand axis."""
    block = _chart(
        kind="combo",
        format="currency",
        unit="EUR",
        categories=["Q1 25", "Q2 25", "Q3 25", "Q4 25", "Q1 26", "Q2 26", "Q3 26"],
        series=[
            {"name": "Net new ARR", "kind": "bar", "values": [1.56e6, 1.95e6, 4.1e6, 1.6e6, 2.8e6, 1.5e6, 2.2e6]},
            {"name": "Burn", "kind": "bar", "values": [1.4e6, 1.99e6, 1.7e6, 2.98e6, 2.99e6, 2.8e6, 3.51e6]},
            {"name": "Burn multiple", "kind": "line", "axis": "secondary", "values": BURN_MULTIPLE},
        ],
        secondary_format="number",
        secondary_unit="x",
        secondary_decimals=2,
    )
    block.update(overrides)
    return block


def test_a_chart_sets_the_decimals_of_each_value_axis():
    """The burn multiple reads 1.27x on the
    chart as on the KPI tile beside it, in a column of the slide as the deck
    has it, and on a dashboard panel."""
    chart = _burn_and_net_new_arr(decimals=1)
    tile = {"type": "metric", "value": "1.27x", "label": "Burn multiple"}
    columns = {"type": "columns", "columns": [[tile, chart], [{"type": "text", "content": "Q3"}]]}
    spec = validate_spec(_deck(_content(columns), _dashboard(panels=[{"block": chart}])))
    body, panel = spec["slides"][0]["blocks"][0]["columns"][0][1], spec["slides"][1]["panels"][0]["block"]
    assert body == panel
    assert (body["decimals"], body["secondary_decimals"]) == (1, 2)
    assert body["series"][2]["values"] == BURN_MULTIPLE


@pytest.mark.parametrize("decimals", [0, 1, 2, 3])
def test_every_count_of_decimals_from_none_to_three_is_kept(decimals):
    stored = validate_spec(_deck(_content(_two_units(decimals=decimals, secondary_decimals=decimals))))
    block = stored["slides"][0]["blocks"][0]
    assert (block["decimals"], block["secondary_decimals"]) == (decimals, decimals)


def test_a_chart_that_sets_no_decimals_stores_none():
    """Unset is the default precision (two significant digits under 10), not a
    count written into every stored chart."""
    plain = validate_spec(_deck(_content(_chart())))["slides"][0]["blocks"][0]
    assert not {"decimals", "secondary_decimals"} & set(plain)


@pytest.mark.parametrize("field", ["decimals", "secondary_decimals"])
@pytest.mark.parametrize("value", [-1, 4, 1.5, "2"])
def test_decimals_outside_none_to_three_are_refused_and_the_error_names_the_field(field, value):
    chart = _burn_and_net_new_arr(**{field: value})
    in_a_column = _content({"type": "columns", "columns": [[{"type": "metric", "value": "1.27x", "label": "Burn"}, chart]]})
    message = _refused(_deck({"layout": "title", "title": "T"}, {"layout": "title", "title": "U"}, in_a_column))
    where = "(layout=content, block=columns, block=chart)"
    assert f"slides.2.blocks.0.columns.0.1.{field} {where}: Input should be 0, 1, 2 or 3" in message


def _bridge(**overrides) -> dict:
    """Two rises, a fall under a million, the ending."""
    block = _chart(
        kind="waterfall",
        format="currency",
        unit="EUR",
        decimals=2,
        categories=["Licences", "Services", "Churn", "Ending"],
        series=[{"name": "ARR", "values": [2_500_000, 1_250_000, -250_000, 3_500_000]}],
        totals=[3],
    )
    block.update(overrides)
    return block


@pytest.mark.parametrize("scale", ["k", "M", "B"])
def test_a_chart_pins_the_unit_scale_of_each_axis(scale):
    """``unit_scale`` writes every currency amount of the chart in one
    letter, ``secondary_unit_scale`` those of the right-hand axis - kept as
    written, in a body and on a dashboard panel."""
    bridge = _bridge(unit_scale=scale)
    two = _two_units(secondary_unit_scale=scale)
    spec = validate_spec(_deck(_content(bridge, two), _dashboard(panels=[{"block": bridge}])))
    body, panel = spec["slides"][0]["blocks"][0], spec["slides"][1]["panels"][0]["block"]
    assert body == panel
    assert body["unit_scale"] == scale
    assert spec["slides"][0]["blocks"][1]["secondary_unit_scale"] == scale


def test_auto_is_stored_as_no_unit_scale():
    """``auto`` is what an unset scale draws: a chart naming it saves exactly
    as one that does not, so nothing changes for a deck that never set it -
    on the right-hand axis too, with or without a series there."""
    plain = validate_spec(_deck(_content(_bridge())))["slides"][0]["blocks"][0]
    named = validate_spec(_deck(_content(_bridge(unit_scale="auto"))))["slides"][0]["blocks"][0]
    assert named == plain
    assert not {"unit_scale", "secondary_unit_scale"} & set(plain)
    one_axis = validate_spec(_deck(_content(_chart(secondary_unit_scale="auto"))))["slides"][0]["blocks"][0]
    assert "secondary_unit_scale" not in one_axis
    two = validate_spec(_deck(_content(_two_units(secondary_unit_scale="auto"))))["slides"][0]["blocks"][0]
    assert "secondary_unit_scale" not in two


def test_a_unit_scale_where_it_has_no_effect_is_kept_not_refused():
    """A pie, a donut, a gauge, a number and a percent ignore the scale - the
    inspector says so - and a chart switched to one of them still saves."""
    for chart in (
        _chart(kind="pie", format="currency", unit_scale="M"),
        _chart(kind="donut", unit_scale="k"),
        _chart(
            kind="gauge",
            categories=["Coverage"],
            series=[{"name": "C", "values": [86]}],
            format="percent",
            unit_scale="M",
        ),
        _chart(format="number", unit_scale="M"),
        _chart(format="percent", unit_scale="B"),
    ):
        stored = validate_spec(_deck(_content(chart)))["slides"][0]["blocks"][0]
        assert stored["unit_scale"] == chart["unit_scale"]


@pytest.mark.parametrize("field", ["unit_scale", "secondary_unit_scale"])
@pytest.mark.parametrize("value", ["m", "K", "T", 1000])
def test_a_unit_scale_outside_auto_k_m_b_is_refused_with_the_letters_allowed(field, value):
    chart = _two_units(**{field: value})
    message = _refused(_deck(_content(chart)))
    assert f"slides.0.blocks.0.{field} (layout=content, block=chart): Input should be 'auto', 'k', 'M' or 'B'" in message


def test_chart_flags_apply_to_their_kinds():
    spec = validate_spec(_deck(_content(_chart(kind="stacked_bar"))))
    assert spec["slides"][0]["blocks"][0]["stacked"] is True, "stacked_bar implies stacked"
    assert "stacked only applies to bar / area" in _refused(_deck(_content(_chart(kind="line", stacked=True))))
    assert "horizontal only applies to bar / stacked_bar" in _refused(
        _deck(_content(_chart(kind="pie", categories=["a", "b", "c"], horizontal=True)))
    )
    assert "goal_label names the target" in _refused(_deck(_content(_chart(goal_label="Plan"))))
    assert "unit" in _refused(_deck(_content(_chart(unit="a unit far too long"))))


def test_chart_caps_and_number_hygiene():
    values = [1, 2, 3]
    seven = [{"name": f"S{index}", "values": values} for index in range(7)]
    assert "series" in _refused(_deck(_content(_chart(series=seven))))
    many = [f"C{index}" for index in range(17)]
    assert "categories" in _refused(_deck(_content(_chart(categories=many, series=[{"name": "S", "values": list(range(17))}]))))
    # A category typed as a number is its text; a non-finite value is refused.
    spec = validate_spec(_deck(_content(_chart(categories=[2024, 2025.0, "2026"]))))
    assert spec["slides"][0]["blocks"][0]["categories"] == ["2024", "2025", "2026"]
    assert "values" in _refused(_deck(_content(_chart(series=[{"name": "S", "values": [1, float("nan"), 3]}]))))
    assert "values" in _refused(_deck(_content(_chart(series=[{"name": "S", "values": [1, float("inf"), 3]}]))))
    assert "categories" in _refused(_deck(_content(_chart(categories=["Q1", "  ", "Q3"]))))
    assert "categories" in _refused(_deck(_content(_chart(categories=["a" * 25, "Q2", "Q3"]))))


#: The ARR bridge of the Q3 2026 quarterly review: opening ARR, new logos,
#: expansion, churn, ending ARR - 16.5 + 1.8 + 1.3 - 0.4 = 19.2.
_ARR_STEPS = [16_500_000, 1_800_000, 1_300_000, -400_000]


def _bridge(ending: float = 19_200_000, **overrides) -> dict:
    bridge = {
        "kind": "waterfall",
        "categories": ["Opening ARR", "New logos", "Expansion", "Churn", "Closing ARR"],
        "series": [{"name": "ARR", "values": [*_ARR_STEPS, ending]}],
        "totals": [0, 4],
        "format": "currency",
    }
    return _chart(**{**bridge, **overrides})


def test_the_quarter_over_quarter_arr_bridge_validates_with_its_defaults_spelled_out():
    """The acceptance bridge saves, and the stored block says how it
    is read - the totals sorted, the tolerance and allow_gap materialised - so
    the renderer never guesses."""
    spec = validate_spec(_deck(_content(_bridge(totals=[4, 0, 4]))))
    block = spec["slides"][0]["blocks"][0]
    assert block["kind"] == "waterfall"
    assert block["totals"] == [0, 4]
    assert block["tolerance"] == 0.5
    assert block["allow_gap"] is False
    # In a dashboard panel too, where a board slide puts it.
    validate_spec(_deck(_dashboard(panels=[{"title": "ARR bridge", "span": 2, "block": _bridge()}])))


def test_a_bridge_that_does_not_add_up_is_refused_with_the_gap_named():
    """Ending ARR 19.14M against steps summing to 19.2M: the board member's
    addition fails, so the save does - naming the total, both figures and
    the gap, in the chart's own currency, and the two ways out."""
    message = _refused(_deck(_content(_bridge(ending=19_140_000))))
    assert "slides.0.blocks.0" in message and "block=chart" in message
    assert '"Closing ARR" (categories.4) \u20ac19.14M vs running sum \u20ac19.2M, gap \u20ac0.06M' in message
    assert "tolerance 0.5% of the \u20ac3.5M the steps moved" in message
    assert "set allow_gap: true" in message and '"Unexplained" step' in message
    # A unit of the chart's own: the figures carry it as the slide would.
    dollars = _refused(_deck(_content(_bridge(ending=19_140_000, unit="USD"))))
    assert "$19.14M vs running sum $19.2M, gap $0.06M" in dollars


def test_the_tolerance_is_a_share_of_what_the_steps_moved_never_of_the_level():
    """0.5% of a 19M ending would be 0.10M - enough to hide the 0.06M miss.
    The slack is 0.5% of the 3.5M the steps moved (17.5k): rounding, not a
    churned logo. A wider tolerance is the author's call, within its cap."""
    validate_spec(_deck(_content(_bridge(ending=19_215_000))))  # 15k off: rounding
    assert "gap \u20ac0.02M" in _refused(_deck(_content(_bridge(ending=19_220_000))))
    validate_spec(_deck(_content(_bridge(ending=19_140_000, tolerance=3))))
    assert "tolerance" in _refused(_deck(_content(_bridge(tolerance=6))))
    assert "tolerance" in _refused(_deck(_content(_bridge(tolerance=-1))))
    # Zero tolerance still forgives float noise (16.5 + 1.8 + 1.3 - 0.4 in floats).
    validate_spec(_deck(_content(_bridge(tolerance=0))))


def test_allow_gap_saves_a_bridge_that_does_not_add_up():
    """The renderer draws the miss as an "Unexplained" step; the spec keeps
    the author's figures as they are."""
    spec = validate_spec(_deck(_content(_bridge(ending=19_140_000, allow_gap=True))))
    block = spec["slides"][0]["blocks"][0]
    assert block["allow_gap"] is True
    assert block["series"][0]["values"][-1] == 19_140_000
    assert block["categories"][-1] == "Closing ARR"


def test_every_total_is_checked_against_the_steps_since_the_previous_one():
    """A subtotal is a level of its own: each run of steps is held to its own
    total, a miss is reported once, and every miss of the chart is named."""
    categories = ["Opening", "New", "Churn", "H1", "New", "Churn", "H2"]
    good = [100, 20, -5, 115, 10, -2, 123]
    validate_spec(_deck(_content(_chart(kind="waterfall", categories=categories, series=[{"name": "ARR", "values": good}], totals=[0, 3, 6]))))
    both = [100, 20, -5, 118, 10, -2, 130]
    message = _refused(
        _deck(_content(_chart(kind="waterfall", categories=categories, series=[{"name": "ARR", "values": both}], totals=[0, 3, 6])))
    )
    assert '"H1" (categories.3) 118 vs running sum 115, gap 3' in message
    # H2 is held to H1 as stated (118 + 8 = 126), not to the running sum before it.
    assert '"H2" (categories.6) 130 vs running sum 126, gap 4' in message
    # A bridge that opens on a step starts from zero; no totals checks nothing.
    validate_spec(_deck(_content(_chart(kind="waterfall", series=[{"name": "Delta", "values": [5, -2, 4]}]))))


def test_two_totals_with_no_step_between_them_must_be_equal_and_a_miss_says_so():
    """Nothing moved between them, so nothing can absorb a difference:
    equal adjacent totals pass (a quarter's ending, the next one's opening),
    a different one is refused naming both totals - never "tolerance 0.5% of
    the EUR 0 the steps moved" - and allow_gap still draws it."""
    categories = ["Q3 ending", "Q4 opening", "New logos", "Q4 ending"]
    equal = _chart(
        kind="waterfall", categories=categories, series=[{"name": "ARR", "values": [100, 100, 5, 105]}], totals=[0, 1, 3]
    )
    validate_spec(_deck(_content(equal)))
    moved = {**equal, "series": [{"name": "ARR", "values": [100, 104, 5, 109]}]}
    message = _refused(_deck(_content(moved)))
    assert '"Q4 opening" (categories.1) 104 vs the previous total "Q3 ending" (categories.0) 100, gap 4' in message
    assert "no step between the two totals explains it" in message
    assert "the steps moved" not in message
    validate_spec(_deck(_content({**moved, "allow_gap": True})))
    # Zero steps between two totals move nothing either.
    flat = _chart(
        kind="waterfall",
        categories=["Opening", "Contraction", "Ending"],
        series=[{"name": "ARR", "values": [100, 0, 103]}],
        totals=[0, 2],
    )
    assert "no step between the two totals explains it" in _refused(_deck(_content(flat)))


def test_a_gap_too_small_for_the_figures_tier_is_written_in_its_own():
    """A 10-unit miss between two billion-scale totals is not "gap EUR 0B"."""
    block = _chart(
        kind="waterfall",
        categories=["Opening", "Ending"],
        series=[{"name": "Cash", "values": [2_500_000_000, 2_500_000_010]}],
        totals=[0, 1],
        format="currency",
    )
    message = _refused(_deck(_content(block)))
    assert "\u20ac2.5B vs the previous total" in message and "gap \u20ac10 -" in message
    assert "\u20ac0B" not in message


def test_a_waterfall_is_one_series_of_steps_with_totals_inside_the_chart():
    two = [{"name": "A", "values": [1, 2, 3]}, {"name": "B", "values": [1, 2, 3]}]
    assert "a waterfall plots one series of changes" in _refused(_deck(_content(_chart(kind="waterfall", series=two))))
    assert "totals names category 3 but the chart has 3 categories (0-2)" in _refused(
        _deck(_content(_chart(kind="waterfall", totals=[0, 3])))
    )
    assert "totals.0" in _refused(_deck(_content(_chart(kind="waterfall", totals=[-1]))))
    assert "stacked only applies to bar / area" in _refused(_deck(_content(_chart(kind="waterfall", stacked=True))))
    assert "horizontal only applies to bar / stacked_bar" in _refused(
        _deck(_content(_chart(kind="waterfall", horizontal=True)))
    )
    # A plan line across the bridge is an axis chart's target.
    validate_spec(_deck(_content(_bridge(target=19_600_000, goal_label="Plan"))))


#: Individual contributors against managers per seniority level.
_PYRAMID = {
    "type": "chart",
    "kind": "butterfly",
    "categories": ["Executive", "Director", "Manager", "Lead", "Senior", "Mid", "Entry"],
    "series": [
        {"name": "Contributors", "values": [0, 2, 10, 40, 80, 30, 15]},
        {"name": "Managers", "values": [5, 20, 18, 6, 0, 0, 0]},
    ],
    "show_values": True,
}


def test_the_seniority_pyramid_saves_on_a_slide_and_in_a_panel_and_stores_nothing_it_did_not_say():
    """The acceptance block saves where a deck puts it, and its own
    fields are never materialised: a butterfly that names none stores none,
    so the renderer reads the defaults (a centre spine, one extent, no totals)."""
    block = validate_spec(_deck(_content(_PYRAMID)))["slides"][0]["blocks"][0]
    assert block["kind"] == "butterfly"
    assert block["series"][0]["values"] == [0, 2, 10, 40, 80, 30, 15]
    assert not {"spine", "symmetric", "show_totals"} & set(block)
    validate_spec(_deck(_dashboard(panels=[{"title": "Seniority mix", "block": _PYRAMID}])))
    named = validate_spec(_deck(_content({**_PYRAMID, "spine": "left", "symmetric": False, "show_totals": True})))
    assert named["slides"][0]["blocks"][0] | {"spine": "left", "symmetric": False, "show_totals": True} == named["slides"][0]["blocks"][0]
    assert "spine" in _refused(_deck(_content({**_PYRAMID, "spine": "right"})))


def test_a_butterfly_is_refused_a_third_series_and_a_negative_value_with_the_slide_and_the_field_named():
    """Two series, one per side, each value a size: the acceptance's two
    refusals name the slide, the block and the field the agent patches."""
    third = {**_PYRAMID, "series": [*_PYRAMID["series"], {"name": "Executives", "values": [1, 0, 0, 0, 0, 0, 0]}]}
    message = _refused(_deck(_content(third)))
    assert "slides.0.blocks.0" in message and "block=chart" in message
    assert "series has 3 entries but a butterfly plots exactly 2" in message
    assert "series has 1 entry but" in _refused(_deck(_content({**_PYRAMID, "series": _PYRAMID["series"][:1]})))
    signed = {**_PYRAMID, "series": [_PYRAMID["series"][0], {"name": "Managers", "values": [5, 20, -18, 6, 0, 0, 0]}]}
    message = _refused(_deck(_content(signed)))
    assert "slides.0.blocks.0" in message and "block=chart" in message
    assert "series.1.values.2 is -18: a butterfly reads both sides as positive sizes" in message
    assert "write 18" in message
    # In a dashboard panel, the panel's path.
    assert "slides.0.panels.0.block" in _refused(_deck(_dashboard(panels=[{"title": "Mix", "block": signed}])))


def test_a_butterfly_has_no_reference_line_no_pinned_axis_and_no_switch_of_a_bar_chart():
    """What a butterfly cannot draw is refused by name, never dropped: a
    target, a pinned axis range, a stack, a horizontal switch, a right-hand
    axis, a combo's series kind. A series' own show_values is a side's."""
    assert "a butterfly has none" in _refused(_deck(_content({**_PYRAMID, "target": 50})))
    for field in ("y_min", "y_max"):
        message = _refused(_deck(_content({**_PYRAMID, field: 100 if field == "y_max" else -10})))
        assert f"{field} pins one value axis, and a butterfly reads two sides" in message, field
    assert "stacked only applies to bar / area" in _refused(_deck(_content({**_PYRAMID, "stacked": True})))
    assert "always run sideways from its spine" in _refused(_deck(_content({**_PYRAMID, "horizontal": True})))
    secondary = [{**_PYRAMID["series"][0], "axis": "secondary"}, _PYRAMID["series"][1]]
    assert "series.0.axis only applies" in _refused(_deck(_content({**_PYRAMID, "series": secondary, "secondary_format": "number"})))
    drawn = [{**_PYRAMID["series"][0], "kind": "bar"}, _PYRAMID["series"][1]]
    assert "series.0.kind only applies to a combo" in _refused(_deck(_content({**_PYRAMID, "series": drawn})))
    quiet = [_PYRAMID["series"][0], {**_PYRAMID["series"][1], "show_values": False}]
    validate_spec(_deck(_content({**_PYRAMID, "series": quiet})))


def test_a_butterfly_s_fields_are_kept_but_change_nothing_on_another_kind():
    """Saved as written, like a unit scale on a pie: a butterfly switched to a
    bar in the inspector keeps its spine, its symmetry and its totals, and
    takes them back when it is a butterfly again."""
    as_bar = validate_spec(_deck(_content({**_PYRAMID, "kind": "bar", "spine": "left", "show_totals": True})))
    block = as_bar["slides"][0]["blocks"][0]
    assert block["kind"] == "bar" and block["spine"] == "left" and block["show_totals"] is True


def test_the_bridge_fields_of_a_chart_that_is_no_longer_a_waterfall_are_dropped():
    """A waterfall switched to another kind in the inspector keeps the bridge
    it was saved with, and `totals` has no control there: the save drops them
    rather than leave the person no way to save the chart."""
    stored = validate_spec(_deck(_content(_bridge())))["slides"][0]["blocks"][0]
    as_bar = validate_spec(_deck(_content({**stored, "kind": "bar"})))["slides"][0]["blocks"][0]
    assert as_bar["kind"] == "bar"
    for field in ("totals", "tolerance", "allow_gap"):
        assert field not in as_bar, field
    # And no other chart ever carries them.
    plain = validate_spec(_deck(_content(_chart())))["slides"][0]["blocks"][0]
    assert not {"totals", "tolerance", "allow_gap"} & set(plain)


@pytest.mark.parametrize(
    ("chart", "figures"),
    [
        ({"format": "number", "unit": "users"}, "1,230 users vs running sum 1,200 users, gap 30 users"),
        ({"format": "number", "unit": "h"}, "1,230h vs running sum 1,200h, gap 30h"),
        ({"format": "percent"}, "1,230% vs running sum 1,200%, gap 30%"),
        ({"format": "currency", "unit": "CHF"}, "CHF 1.23k vs running sum CHF 1.2k, gap CHF 0.03k"),
    ],
    ids=["users", "hours", "percent", "chf"],
)
def test_the_gap_is_written_the_way_the_chart_writes_a_value(chart, figures):
    block = _chart(kind="waterfall", series=[{"name": "S", "values": [1000, 200, 1230]}], totals=[0, 2], **chart)
    assert figures in _refused(_deck(_content(block)))


def test_waterfall_gaps_is_the_rule_the_validator_applies():
    from app.services.presentation_spec import waterfall_gaps

    (miss,) = waterfall_gaps([*_ARR_STEPS, 19_140_000], [0, 4], 0.5)
    assert (miss.index, miss.total, miss.moved) == (4, 19_140_000, 3_500_000)
    assert miss.running == pytest.approx(19_200_000)
    assert miss.gap == pytest.approx(-60_000)
    assert waterfall_gaps([*_ARR_STEPS, 19_200_000], [0, 4], 0.5) == []
    # The opening total has nothing before it to miss.
    assert waterfall_gaps([42, 1, 43], [0, 2], 0) == []
    # A miss names the total it was measured from; a bridge opening on a step has none.
    (adjacent,) = waterfall_gaps([100, 104], [0, 1], 0.5)
    assert (adjacent.since, adjacent.moved) == (0, 0)
    (from_zero,) = waterfall_gaps([5, 6], [1], 0.5)
    assert from_zero.since is None


def test_a_dashboard_is_a_tile_row_over_a_two_row_grid():
    spec = validate_spec(_deck(_dashboard()))
    (slide,) = spec["slides"]
    assert slide["layout"] == "dashboard" and [panel.get("span", 1) for panel in slide["panels"]] == [2, 1]
    # Six single panels or two full-width ones fill the grid exactly...
    validate_spec(_deck(_dashboard(panels=[{"block": _chart()} for _ in range(6)])))
    validate_spec(_deck(_dashboard(panels=[{"span": 3, "block": _chart()}, {"span": 3, "block": _chart()}])))
    # ...while three half-width panels need a third row under CSS auto-placement.
    message = _refused(_deck(_dashboard(panels=[{"span": 2, "block": _chart()} for _ in range(3)])))
    assert "panels need 3 rows" in message and "spans 2, 2, 2" in message and "layout=dashboard" in message
    assert "panels" in _refused(_deck(_dashboard(panels=[])))
    assert "panels" in _refused(_deck(_dashboard(panels=[{"block": _chart()} for _ in range(7)])))
    assert "tiles" in _refused(_deck(_dashboard(tiles=[{"value": "1", "label": "Alone"}])))
    assert "tiles" in _refused(_deck(_dashboard(tiles=[{"value": str(n), "label": f"Tile {n}"} for n in range(9)])))
    # A panel shows a figure: a text block is not one.
    message = _refused(_deck(_dashboard(panels=[{"block": {"type": "text", "content": "words"}}])))
    assert "panels.0.block" in message and "chart" in message and "table" in message


#: Grouped bars over eight quarters, one value below zero, and a 3 x 4 table.
_HALF_AND_HALF = [
    {
        "title": "Net new ARR by quarter",
        "block": _chart(
            categories=["Q1 25", "Q2 25", "Q3 25", "Q4 25", "Q1 26", "Q2 26", "Q3 26", "Q4 26"],
            series=[
                {"name": "New logos", "values": [1.5, 1.8, 1.7, 2.4, 2.7, 2.5, 3.2, 3.4]},
                {"name": "Expansion", "values": [0.8, 0.9, -0.3, 1.1, 1.3, 1.2, 1.6, 1.8]},
            ],
        ),
    },
    {
        "title": "Top accounts",
        "block": {
            "type": "table",
            "headers": ["Account", "Region", "ARR"],
            "rows": [["Acme", "EMEA", "2.4M"], ["Globex", "NA", "1.9M"], ["Initech", "APAC", "1.2M"], ["Umbrella", "EMEA", "0.9M"]],
        },
    },
]
_SIX_TILES = [{"value": str(10 * n), "label": f"KPI {n}"} for n in range(1, 7)]


def test_a_dashboard_grid_takes_two_equal_columns():
    """`panel_columns`: 3 by default - the grid every dashboard had -
    or 2 equal columns, where a span is 1 or 2 and the two-row cap holds."""
    # Absent, it reads as the three columns every stored dashboard was drawn on.
    (slide,) = validate_spec(_deck(_dashboard()))["slides"]
    assert slide["panel_columns"] == DASHBOARD_GRID_COLUMNS == 3
    assert DASHBOARD_PANEL_COLUMNS == (2, 3)
    # The slide of the issue: six tiles over a chart and a table, half and half.
    (slide,) = validate_spec(_deck(_dashboard(tiles=_SIX_TILES, panel_columns=2, panels=_HALF_AND_HALF)))["slides"]
    assert slide["panel_columns"] == 2 and [panel.get("span", 1) for panel in slide["panels"]] == [1, 1]
    # A full-width span is 2 there, and four single panels fill the two rows...
    validate_spec(_deck(_dashboard(panel_columns=2, panels=[{"span": 2, "block": _chart()}, {"span": 2, "block": _chart()}])))
    validate_spec(_deck(_dashboard(panel_columns=2, panels=[{"block": _chart()} for _ in range(4)])))
    # ...while a fifth needs a third row, which the three columns would have held.
    message = _refused(_deck(_dashboard(panel_columns=2, panels=[{"block": _chart()} for _ in range(5)])))
    assert "panels need 3 rows of the 2-column grid" in message and "spans 1, 1, 1, 1, 1" in message
    assert "set panel_columns to 3" in message and "layout=dashboard" in message
    validate_spec(_deck(_dashboard(panels=[{"block": _chart()} for _ in range(5)])))
    # A span of 3 is wider than two columns: refused, naming the field and the fix.
    message = _refused(_deck(_dashboard(panel_columns=2, panels=[{"span": 3, "block": _chart()}, {"block": _chart()}])))
    assert "panels.0.span (3) is wider than the panel grid" in message
    assert "panel_columns is 2, so a panel spans 1 or 2 columns" in message and "set panel_columns to 3" in message
    message = _refused(
        _deck(_dashboard(panel_columns=2, panels=[{"span": 3, "block": _chart()}, {"block": _chart()}, {"span": 3, "block": _chart()}]))
    )
    assert "panels.0.span (3), panels.2.span (3) are wider than the panel grid" in message
    # 2 and 3, nothing else - not a third value, not a numeral in a string.
    for wrong in (1, 4, 0, "2"):
        assert "panel_columns" in _refused(_deck(_dashboard(panel_columns=wrong))), wrong


def test_the_renderer_draws_the_columns_the_validator_counts():
    """The frontend mirrors the grid the validator simulates: the default
    column count, one rule per other count in the stylesheet, the inspector's
    choices and the type."""
    dashboard = _read("layouts", "dashboard.tsx")
    assert f"const DEFAULT_GRID_COLUMNS = {DASHBOARD_GRID_COLUMNS};" in dashboard
    assert "const columns = slide.panel_columns ?? DEFAULT_GRID_COLUMNS;" in dashboard
    assert '<div className="deck-dashboard-panels" data-columns={columns}>' in dashboard
    css = _read("presentation.css")
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in css
    for columns in DASHBOARD_PANEL_COLUMNS:
        if columns != DASHBOARD_GRID_COLUMNS:
            rule = css.split(f'.deck-root .deck-dashboard-panels[data-columns="{columns}"] {{', 1)[1].split("}", 1)[0]
            assert f"grid-template-columns: repeat({columns}, minmax(0, 1fr));" in rule
    choices = ", ".join(map(str, DASHBOARD_PANEL_COLUMNS))
    assert f"export const DASHBOARD_PANEL_COLUMNS = [{choices}] as const;" in _read("slide-schema.ts")
    types = (FRONTEND_ROOT / "src" / "types" / "index.ts").read_text(encoding="utf-8")
    assert f"export type DeckDashboardPanelColumns = {' | '.join(map(str, DASHBOARD_PANEL_COLUMNS))};" in types
    assert "panel_columns?: DeckDashboardPanelColumns;" in types


def test_dashboard_panels_take_tables_and_metrics_too():
    table = {"type": "table", "headers": ["Version", "Share"], "rows": [["6.x", "38%"], ["5.x", "62%"]]}
    metric = {"type": "metric", "value": "112", "label": "EE trials", "trend": "up"}
    grid = {"type": "metrics_grid", "items": [{"value": "1", "label": "a", "sparkline": [1, 2, 3]}, {"value": "2", "label": "b"}]}
    spec = validate_spec(
        _deck(
            _dashboard(
                panels=[{"title": "By version", "block": table}, {"block": metric}, {"span": 1, "block": grid}],
                footer_block={"type": "callout", "content": "Source: telemetry, Q4"},
                hide_footer=True,
            )
        )
    )
    (slide,) = spec["slides"]
    assert [panel["block"]["type"] for panel in slide["panels"]] == ["table", "metric", "metrics_grid"]
    assert slide["panels"][2]["block"]["items"][0]["sparkline"] == [1, 2, 3]
    assert slide["footer_block"]["type"] == "callout" and slide["hide_footer"] is True


def test_every_layout_that_can_hide_the_footer_is_one_the_chrome_draws_it_on():
    """``hide_footer`` is an opt-out, so every slide model that carries one must
    be a layout the deck chrome draws the footer bar on (``FOOTER_LAYOUTS`` in
    ``layouts/footer.ts``). A layout missing from that set never shows the
    footer text or the template header, and its ``hide_footer`` is dead - the
    ``dashboard`` shipped that way."""
    from app.services.presentation_spec import SLIDE_LAYOUTS, slide_model

    footer = _read("layouts", "footer.ts")
    match = re.search(r"const FOOTER_LAYOUTS[^=]*=\s*new Set\(\[(.*?)\]\)", footer, re.DOTALL)
    assert match, "footer.ts lost its FOOTER_LAYOUTS set"
    drawn = set(re.findall(r'"([a-z_]+)"', match.group(1)))
    hiders = {layout for layout in SLIDE_LAYOUTS if "hide_footer" in slide_model(layout).model_fields}
    assert "dashboard" in hiders
    assert hiders <= drawn, f"layouts with a dead hide_footer: {sorted(hiders - drawn)}"
    assert drawn <= set(SLIDE_LAYOUTS), f"footer.ts names unknown layouts: {sorted(drawn - set(SLIDE_LAYOUTS))}"


def test_a_kpi_tile_sparkline_is_bounded():
    tile = {"value": "6,250", "label": "Instances", "sparkline": list(range(24))}
    spec = validate_spec(_deck(_dashboard(tiles=[tile, {"value": "1", "label": "b"}])))
    assert len(spec["slides"][0]["tiles"][0]["sparkline"]) == 24
    long = {"value": "6,250", "label": "Instances", "sparkline": list(range(25))}
    assert "tiles.0.sparkline" in _refused(_deck(_dashboard(tiles=[long, {"value": "1", "label": "b"}])))
    # A metric block anywhere carries the same field.
    spec = validate_spec(_deck(_content({"type": "metric", "value": "4h", "label": "MTTD", "sparkline": [9, 7, 4]})))
    assert spec["slides"][0]["blocks"][0]["sparkline"] == [9, 7, 4]


def test_the_reference_documents_charts_panels_and_the_chart_palette():
    reference = spec_reference()
    for word in ("- chart: kind: ChartKind", "- dashboard: title", "panels: DashboardPanel[] (1-6)", "tiles: MetricItem[] (2-8)"):
        assert word in reference, word
    assert "panel_columns?: 2|3 = 3 (columns of the panel grid" in reference
    assert "PanelBlock = chart/table/metric/metrics_grid/org_chart" in reference
    assert "sparkline?: number[] (2-24)" in reference
    assert (
        '- ChartKind: "bar"|"stacked_bar"|"line"|"area"|"pie"|"donut"|"gauge"|"combo"|"waterfall"|"butterfly"|"map"'
        in reference
    )
    for field in ("totals?: int[] (0-16) (waterfall only", "tolerance?: number (waterfall only", "allow_gap?: bool"):
        assert field in reference, field
    # A butterfly's own fields, its spine's two places spelled inline.
    for field in ('spine?: "center"|"left" (butterfly only', "symmetric?: bool (butterfly only", "show_totals?: bool (butterfly only"):
        assert field in reference, field
    # The right-hand axis of a two-unit chart, documented from the models like the rest.
    assert '- SeriesAxis: "primary"|"secondary"' in reference
    for field in ("axis?: SeriesAxis", "secondary_format?: ValueFormat", "secondary_unit?: text<=8", "secondary_target?: number"):
        assert field in reference, field
    # The decimals of each axis read as the four counts they may be.
    for field in ("decimals?: 0|1|2|3 (every value written", "secondary_decimals?: 0|1|2|3"):
        assert field in reference, field
    # The unit scale of each axis, with the letters it may be - in the tool description too.
    from app.services.presentation_guidance import (
        build_generate_presentation_description,
        build_presentation_reference_text,
    )

    assert build_generate_presentation_description()
    description = build_presentation_reference_text()
    for field in (
        'unit_scale?: "auto"|"k"|"M"|"B" (currency on an axis chart',
        'secondary_unit_scale?: "auto"|"k"|"M"|"B"',
    ):
        assert field in reference, field
        assert field in description, field
    from app.services.presentations import CHART_THEME_CONFIG_KEYS

    assert tuple(f"chart_{index}" for index in range(1, 7)) == CHART_THEME_CONFIG_KEYS
    assert set(CHART_THEME_CONFIG_KEYS) <= set(THEME_CONFIG_KEYS)


# ── Frontend tripwires ──────────────────────────────────────────────────────


def test_frontend_shows_a_failed_save_once_and_line_by_line():
    """The editor's autosave and the share dialog both toast a failed PATCH
    under their own title, so the app-wide "Error" toast must stay out of it or
    the same 422 shows twice. The human message lists one problem per
    line, which the toast must not collapse back into a paragraph."""
    hook = (FRONTEND_ROOT / "src" / "hooks" / "api" / "usePresentationApi.ts").read_text(encoding="utf-8")
    update = hook.split("export function useUpdatePresentation()", 1)[1].split("\nexport function", 1)[0]
    assert "meta: { suppressGlobalErrorToast: true }" in update
    for component in ("useDeckAutosave.ts", "PresentationShareDialog.tsx"):
        source = _read(component)
        assert "useUpdatePresentation()" in source
        assert "err instanceof Error ? err.message" in source, component
    toast = (FRONTEND_ROOT / "src" / "components" / "ui" / "toast.tsx").read_text(encoding="utf-8")
    description = toast.split("description={", 1)[1].split("\n", 1)[0]
    assert "whitespace-pre-line" in description


def test_frontend_renders_every_layout_in_the_catalog():
    """A layout with no component renders as a blank slide."""
    source = _read("layouts", "index.tsx")
    missing = [layout for layout in SLIDE_LAYOUTS if f'case "{layout}":' not in source]
    assert missing == [], f"layouts/index.tsx has no case for: {', '.join(missing)}"


def test_frontend_renders_every_block_in_the_catalog():
    """A block with no component renders as an invisible gap."""
    source = _read("blocks", "index.tsx")
    missing = [block for block in BLOCK_TYPES if f'case "{block}":' not in source]
    assert missing == [], f"blocks/index.tsx has no case for: {', '.join(missing)}"


def test_frontend_maps_every_theme_key():
    """A whitelisted theme key with no CSS variable is stored, served and
    silently ignored — the preview would then lie about the palette."""
    # `theme.ts` is the ONE key -> CSS variable map, shared by the interactive
    # deck and the static render page behind the export.
    theme = _read("theme.ts")
    # The preview forwards the keys of `palette-keys.ts`, the list the
    # template form's colour groups are cut from too.
    palette_keys = _read("palette-keys.ts")
    missing_theme = [k for k in THEME_CONFIG_KEYS if f"{k}:" not in theme]
    missing_preview = [k for k in THEME_CONFIG_KEYS if f'"{k}"' not in palette_keys]
    assert missing_theme == [], f"theme.ts maps no CSS variable for: {missing_theme}"
    assert missing_preview == [], f"palette-keys.ts forwards no value for: {missing_preview}"
    assert 'import { THEME_CONFIG_KEYS } from "./palette-keys";' in _read("previewSpec.ts")
    static_page = (FRONTEND_ROOT / "src" / "pages" / "PresentationStaticRender.tsx").read_text(encoding="utf-8")
    for name, source in (
        ("PresentationDeck.tsx", _read("PresentationDeck.tsx")),
        ("PresentationStaticRender.tsx", static_page),
    ):
        assert "themeStyle(" in source, f"{name} does not apply the shared themeStyle()"


def test_frontend_preview_knows_every_layout():
    # The catalog moved to layout-options.ts with the editor's layout picker;
    # the preview reads it from there instead of carrying a copy.
    # One list: there is no legacy tier to add to it.
    source = _read("layout-options.ts")
    start = source.index("const SLIDE_LAYOUTS")
    known = source[start : source.index("];", start)]
    missing = [layout for layout in SLIDE_LAYOUTS if f'"{layout}"' not in known]
    assert missing == [], f"layout-options.ts SLIDE_LAYOUTS lacks: {missing}"
    extra = [layout for layout in RETIRED_LAYOUTS if f'"{layout}"' in known]
    assert extra == [], f"layout-options.ts still lists the retired: {extra}"
    assert "LEGACY_LAYOUTS" not in source and "CURRENT_LAYOUTS" not in source
    assert 'import { SLIDE_LAYOUTS } from "./layout-options"' in _read("previewSpec.ts")


def test_frontend_markup_tokenizer_mirrors_the_backend():
    """Same markers, same span kinds, and never an HTML sink."""
    source = _read("markup.ts")
    for marker in (r"\*\*", "`", r"\[\[", "(?<![A-Za-z0-9_])_"):
        assert marker in source, f"markup.ts tokenizer lacks the {marker!r} construct"
    for kind in MARKUP_SPAN_KINDS:
        assert f'"{kind}"' in source
    renderer = _read("inline-markup.tsx")
    assert "dangerouslySetInnerHTML" not in renderer
    assert "dangerouslySetInnerHTML" not in source


def test_frontend_block_components_never_build_html_from_spec_text():
    """Only the Mermaid output (its own SVG, DOMPurify SVG profile) may reach
    an HTML sink. The v1 ``rich_text`` renderer and its DOMPurify pass left
    with the retired layouts: a deck has no HTML field, so no layout
    component has an HTML sink at all."""
    blocks_dir = PRESENTATION_DIR / "blocks"
    offenders = []
    for path in blocks_dir.glob("*.tsx"):
        text = path.read_text(encoding="utf-8")
        if "dangerouslySetInnerHTML" in text and path.name != "Diagram.tsx":
            offenders.append(path.name)
    assert offenders == []
    layouts_dir = PRESENTATION_DIR / "layouts"
    assert not (layouts_dir / "legacy.tsx").exists(), "the v1 renderers were retired"
    sinks = [path.name for path in layouts_dir.glob("*.tsx") if "dangerouslySetInnerHTML" in path.read_text(encoding="utf-8")]
    assert sinks == []


def test_frontend_diagram_block_is_lazy_and_strict():
    """The Mermaid configuration lives in `diagram-theme.ts` (with the
    deck theme); `Diagram.tsx` runs every render on it and sanitizes the
    output through DOMPurify's SVG profile."""
    media = _read("blocks", "media.tsx")
    assert 'lazy(() => import("./Diagram"))' in media
    config = _read("blocks", "diagram-theme.ts")
    assert 'securityLevel: "strict"' in config
    assert "htmlLabels: false" in config
    diagram = _read("blocks", "Diagram.tsx")
    assert "mermaid.initialize({ ...MERMAID_CONFIG," in diagram
    assert "DOMPurify.sanitize" in diagram


def test_frontend_icons_load_as_one_lazy_catalog_chunk():
    """Neither the whole catalog in the main bundle (top-level `icons`) nor one
    chunk per icon (`lucide-react/dynamic`); see `deck-icons.ts`."""
    loader = _read("deck-icons.ts")
    assert 'import("lucide-react/dist/esm/icons/index.js")' in loader
    assert "lucideExportName" in loader
    icon = _read("DeckIcon.tsx")
    for text in (loader, icon):
        assert 'from "lucide-react/dynamic"' not in text
        assert "dynamicIconImports" not in text
        assert "import { icons" not in text
    # The placeholder the export capture waits on.
    assert "deck-icon-pending" in icon
    assert ".deck-icon-pending" in _read("presentation.css")


def test_frontend_chrome_draws_slide_numbers_inside_the_canvas():
    """reveal's own overlay sits outside the scaled slide and drops out of the
    PDF / PNG capture; the chrome must own it."""
    deck = _read("PresentationDeck.tsx")
    assert "slideNumber: false" in deck
    assert "<DeckChrome" in deck
    chrome = _read("chrome.tsx")
    assert "deck-chrome-number" in chrome


def test_frontend_stylesheet_has_a_rule_for_every_tone():
    css = _read("presentation.css")
    for tone in ("accent", "info", "success", "warning", "error", "muted"):
        assert f'[data-tone="{tone}"]' in css
    # Every theme sets the full palette so a template override has a baseline.
    # ``dark`` is the ``.deck-root`` default; ``light`` is the only other scheme -
    # the old ``filigran`` preset is an alias the backend normalises
    # away, so the stylesheet must not keep a block a deck can never wear.
    assert '[data-deck-theme="filigran"]' not in css
    for theme in ("light",):
        block_start = css.index(f'.deck-root[data-deck-theme="{theme}"]')
        block = css[block_start : css.index("}", block_start)]
        for token in (
            "--deck-bg",
            "--deck-fg",
            "--deck-accent",
            "--deck-muted",
            "--deck-info",
            "--deck-success",
            "--deck-warning",
            "--deck-error",
        ):
            assert token in block, f"{theme} theme lacks {token}"


def test_frontend_toned_heading_colours_its_icon_too():
    """A heading's `tone` colours the heading AND its icon - the icon rule used
    to pin the accent, so a `success` heading kept a blue icon - read
    off the heading itself, never inherited from a toned container. And a
    heading, section header or list item whose whole text is one `[[tone:..]]`
    span takes that tone for its icon / marker, so the natural way to write "a
    green heading" no longer leaves the icon accent beside the coloured text."""
    css = _read("presentation.css")
    assert ".deck-root .deck-heading[data-tone] .deck-heading-icon { color: var(--tone-text); }" in css
    # Untoned headings keep the accent icon (the toned rule refines, never replaces).
    assert ".deck-root .deck-heading-icon { color: var(--deck-accent); }" in css
    markup = _read("markup.ts")
    assert "export function soleSpanTone" in markup
    assert '"gradient"' in markup[markup.index("export function soleSpanTone") :], (
        "a gradient span is a text treatment, never a block tone"
    )
    text = _read("blocks", "text.tsx")
    assert "block.tone ?? soleSpanTone(block.content)" in text  # heading
    assert "block.tone ?? soleSpanTone(block.title)" in text  # section header
    assert "item.tone ?? soleSpanTone(item.text)" in _read("blocks", "list.tsx")
    # Tables stay out: `td[data-tone]` also bolds the cell and a toned header
    # emphasises its whole column, so a span there must stay a text decoration.
    assert "soleSpanTone" not in _read("blocks", "table.tsx")


# ── Deck operation: one toolbar, nothing over the slide ──────────────


def test_frontend_draws_nothing_of_reveal_over_the_slide():
    """Every control lives in `DeckToolbar`; reveal's own on-slide chrome
    (corner arrows, progress bar, help overlay, jump box) stays off."""
    deck = _read("PresentationDeck.tsx")
    for option in ("controls", "progress", "slideNumber", "help", "jumpToSlide"):
        assert f"{option}: false" in deck, f"reveal `{option}` is drawn over the slide"
    # And the stylesheet no longer styles chrome that is never rendered.
    css = _read("presentation.css")
    assert ".reveal .controls" not in css
    assert ".reveal .progress" not in css


def test_frontend_neighbour_slides_survive_the_hidden_preflight():
    """reveal marks every non-current slide `hidden` and shows the loaded
    neighbours through an inline `display: block`; Tailwind's preflight turns
    `[hidden]` into `display: none !important`, which beats a plain inline style.
    Without reveal's `!important` display the overview draws one slide and N-1
    empty frames, and a `slide` transition pushes nothing out. The knob is the
    reveal config, not a stylesheet rule: an unlayered `!important` in
    presentation.css would LOSE to the layered one (important declarations sort
    earlier layers first), and a rule inside `@layer base` would have to pick
    block or none for every hidden slide, losing reveal's own load/unload."""
    deck = _read("PresentationDeck.tsx")
    assert re.search(r'display:\s*"block !important"', deck), (
        "reveal `display` must be `block !important` so a hidden neighbour slide is still drawn"
    )
    css = _read("presentation.css")
    assert "section[hidden]" not in css, "the hidden-slide fix belongs in the reveal config, not the stylesheet"


def test_frontend_every_interactive_deck_goes_through_the_stage():
    """A bare `PresentationDeck` has no way to turn the page once reveal's
    controls are off: every interactive render must be a `DeckStage`, which
    brings the toolbar, fullscreen and key ownership. The print layout and the
    stage itself are the only direct renders, plus the audience window
    (`PresentationAudience.tsx`, xtm-one #10552): a slides-only window shared
    in a meeting, with no toolbar by design, turned by the speaker bridge, its
    own deck keys and click to advance."""
    allowed = {"DeckStage.tsx", "PresentationAudience.tsx"}
    offenders = []
    for path in [*PRESENTATION_DIR.rglob("*.tsx"), *(FRONTEND_ROOT / "src" / "pages").glob("*.tsx")]:
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"<PresentationDeck\b[^>]*?(?:/>|>)", text, re.S):
            if path.name in allowed or 'variant="print"' in match.group(0):
                continue
            offenders.append(f"{path.name}: {match.group(0)[:80]}")
    assert offenders == [], f"bare interactive PresentationDeck: {offenders}"
    # The consumers that exist today do go through the stage.
    for name in ("PresentationCard.tsx", "PreviewDeck.tsx"):
        assert "DeckStage" in _read(name), f"{name} does not render a DeckStage"
    viewer = (FRONTEND_ROOT / "src" / "pages" / "PresentationViewer.tsx").read_text(encoding="utf-8")
    assert "<DeckStage" in viewer


def test_frontend_deck_keys_are_gated_and_documented():
    """Every app-level shortcut is bound through reveal's `keyboard` map (so it
    passes the same input / modifier / owned-region gate as reveal's own keys),
    is listed on the shortcut sheet, and is in the user docs."""
    keys = _read("deck-keys.ts")
    deck = _read("PresentationDeck.tsx")
    assert "keyboardCondition" in deck and "deckMayHandleKey(" in deck
    codes = re.findall(r"^\s+([A-Z0-9]+): \d+,", keys[keys.index("DECK_KEY_CODES") :], re.M)
    assert set(codes) == {"F", "G", "S", "A", "SLASH", "F1", "ESC"}, codes
    for code in codes:
        assert f"[DECK_KEY_CODES.{code}]" in deck, f"DECK_KEY_CODES.{code} is declared but never bound"
    sheet = _read("DeckShortcutsDialog.tsx")
    docs = (BACKEND_ROOT.parent / "docs" / "docs" / "usage" / "presentations.md").read_text(encoding="utf-8")
    for label, doc_key in (('"F"', "++f++"), ('"G"', "++g++"), ('"S"', "++s++"), ('"?"', "++question++"), ('"F1"', "++f1++"), ('"Esc"', "++esc++"), ('"A"', "++a++"), ('"O"', "++o++"), ('"B"', "++b++")):
        assert label in sheet, f"shortcut sheet does not list {label}"
        assert doc_key in docs, f"usage docs do not list {doc_key}"
    # Regions that own their keys: menus, dialogs, listboxes, an open popup's
    # trigger and the explicit opt-out the editor panel uses.
    for region in ('[role="menu"]', '[role="dialog"]', '[role="listbox"]', '[aria-haspopup][aria-expanded="true"]', '[data-deck-keys="off"]'):
        assert region in keys, f"deckMayHandleKey does not exempt {region}"
    assert "DECK_KEYS_OFF_ATTR" in _read("PresentationEditorPanel.tsx")
    assert "DECK_KEYS_OFF_ATTR" in _read("SlideJumpMenu.tsx")


def test_frontend_fullscreen_takes_the_stage_and_hides_the_toolbar_when_idle():
    """Fullscreen is requested on the stage (toolbar included). While
    presenting the deck fills the screen edge to edge on a black stage:
    the toolbar is a transient OVERLAY that leaves after stillness,
    the surface is the whole screen and letterboxes the deck root at the
    deck's own ratio - the deck never letterboxes itself in its own paper.
    The dialogs opened from the bar portal INTO the stage - nothing outside
    the fullscreen element paints."""
    stage = _read("DeckStage.tsx")
    assert "useElementFullscreen(stageRef)" in stage
    assert "data-deck-idle" in stage
    assert "portalContainer" in stage
    # The bar and the menus portaled into the stage read from the dark token
    # set whatever mode the app is in: white icons on a black screen.
    assert 'data-mode={fullscreen.isFullscreen ? "dark" : undefined}' in stage
    # The TOKENS of that subtree are the design system's own dark set, which it
    # declares on a `dark` class - so the stage carries the class next to the
    # attribute rather than the app re-declaring a token set of its own.
    assert re.search(r'fullscreen\.isFullscreen && "dark"', stage), (
        "the presenting stage must carry the library's dark class, or its bar reads light on a light app"
    )
    index_css = (FRONTEND_ROOT / "src" / "index.css").read_text(encoding="utf-8")
    # The forced-dark subtree flips the MODE, never the brand. A custom property
    # holding `var()` is resolved where it is declared and inherited as a value,
    # so the stage would inherit the accent `<html>` resolved from the LIGHT set;
    # the subtree re-declares it to re-resolve against its own dark set - the
    # product accent generically, the XTM One purple under that theme - at a
    # specificity the branding block (0,2,0) still beats.
    assert re.search(r'^\[data-mode="dark"\] \{\s*--color-accent: var\(--color-filigran-brand-primary\);', index_css, re.M), (
        "the forced-dark subtree must re-resolve the product accent, or the presenting bar reads the light-mode value"
    )
    assert re.search(r'^:where\(\[data-theme="xtm-one"\]\) \[data-mode="dark"\] \{\s*--color-accent: var\(--color-filigran-ia-primary\);', index_css, re.M), (
        "under the XTM One theme the presenting bar keeps the purple accent, re-resolved on the subtree"
    )
    assert not re.search(r'^\.dark[^\n{]*\{[^}]*--color-accent\s*:', index_css, re.M | re.S), (
        "a declaration on the bare `dark` class would outrank and shadow the theme's accent on the subtree"
    )
    # An administrator's accent (the unlayered branding block of
    # `usePlatformConfig`) IS declared on the subtree, because the dark class it
    # carries would otherwise reinstate the library's own; its background
    # palette is not, a black stage wants the dark set.
    color_utils = (FRONTEND_ROOT / "src" / "lib" / "color-utils.ts").read_text(encoding="utf-8")
    assert "export const ACCENT_SELECTOR = ':root:root,[data-mode=\"dark\"][data-mode=\"dark\"]';" in color_utils, (
        "the branding accent must be declared on the forced-dark subtree too"
    )
    assert "`${ACCENT_SELECTOR}{${rules.join(\";\")}}`" in color_utils
    assert "`:root:root{${rules.join(\";\")}}`" in color_utils, (
        # Doubled like the accent - it sets the library's own surface tokens,
        # which `.light` / `.dark` also declare - but on the root ALONE.
        "the background palette stays on the root alone"
    )
    # ...and the per-mode Tailwind variants read the subtree, not `<html>`
    # alone: a `light:` override cannot fire inside it, a `dark:` one does.
    for variant in ("light", "dark"):
        declaration = re.search(rf"^@custom-variant {variant} \((.*)\);$", index_css, re.M)
        assert declaration, f"the `{variant}:` custom variant is gone"
        assert '[data-mode="dark"]' in declaration.group(1), (
            f"the `{variant}:` variant must honour a forced-dark subtree ([data-mode=\"dark\"])"
        )
    css = _read("presentation.css")
    assert ".deck-stage:fullscreen .deck-toolbar" in css
    assert '.deck-stage:fullscreen[data-deck-idle="true"] .deck-toolbar' in css
    # The one black of a presented deck is declared once, on the presenting
    # stage, and read by the stage and the surface (the box the letterbox shows).
    stage_rule = re.search(r"\.deck-stage:fullscreen,\s*\.deck-stage\[data-deck-viewport\] \{([^}]*)\}", css)
    assert stage_rule, "the presenting stage rule is gone"
    assert "--deck-stage-bg: #000;" in stage_rule.group(1), "the presenting stage must declare its black once"
    assert "background: var(--deck-stage-bg);" in stage_rule.group(1)
    # The bar is OUT OF FLOW while presenting. It kept a row of its own first
    # so a title set against the top edge was never covered - but on a
    # 16:9 screen the row took its height off a 16:9 deck for the whole
    # presentation, pillarboxing it in the deck's own paper (white for a light
    # deck). The overlay covers the top of the slide only while the bar is up,
    # and the bar leaves after a moment of stillness; the idle hide is visual
    # only (opacity + transform), so the slide never moves or rescales.
    toolbar_rule = re.search(
        r"\.deck-stage:fullscreen \.deck-toolbar,\s*\.deck-stage\[data-deck-viewport\] \.deck-toolbar \{([^}]*)\}",
        css,
    )
    assert toolbar_rule, "the presenting toolbar rule is gone"
    assert "position: absolute;" in toolbar_rule.group(1), "the presenting toolbar must be an overlay, not a row"
    assert "top: 0;" in toolbar_rule.group(1)
    assert re.search(r"background: rgb\(0 0 0 / 0\.\d+\);", toolbar_rule.group(1)), "a dark translucent scrim"
    # The surface is the whole screen: the chat card's aspect box and height
    # cap are undone while presenting. The letterbox itself - the size
    # container, the deck root at the deck's own ratio, the surface painted
    # with `--deck-stage-bg` - is the stage's at rest as well, pinned
    # by `test_frontend_stage_letterboxes_the_deck_in_the_app_background`.
    surface_rule = re.search(
        r"\.deck-stage:fullscreen \.deck-stage-surface,\s*\.deck-stage\[data-deck-viewport\] \.deck-stage-surface \{([^}]*)\}",
        css,
    )
    assert surface_rule, "the presenting surface rule is gone"
    for declaration in ("flex: 1 1 0%;", "height: auto;", "max-height: none;", "aspect-ratio: auto;"):
        assert declaration in surface_rule.group(1), f"the presenting surface lost `{declaration}`"
    idle_rule = re.search(r'\.deck-stage:fullscreen\[data-deck-idle="true"\] \.deck-toolbar([^{]*)\{([^}]*)\}', css)
    assert idle_rule and "opacity: 0;" in idle_rule.group(2) and "translateY(-100%)" in idle_rule.group(2)
    # The idle hide spares a bar that is hovered, that holds the keyboard focus
    # and that holds an OPEN menu: the jump list and the `More` menu render
    # inline in the bar, so hiding it would take a menu being read away with it
    # (the stage's timer runs on under an open menu).
    for guard in (":not(:hover)", ":not(:has(:focus-visible))", ':not(:has([aria-expanded="true"]))'):
        assert guard in idle_rule.group(1), f"the idle hide lost its `{guard}` guard"
    # The pointer leaves with the bar - on the surface and EVERY element in it,
    # because `cursor` is inherited, not cascaded: a slide control with a cursor
    # of its own (the app's `button` rule, a chart legend item, a link) would
    # keep the arrow if the value sat on the stage alone. The bar and the
    # dialogs portaled into the stage keep theirs.
    cursor_rule = re.search(
        r'\.deck-stage:fullscreen\[data-deck-idle="true"\] \.deck-stage-surface,\s*'
        r'\.deck-stage:fullscreen\[data-deck-idle="true"\] \.deck-stage-surface \*,\s*'
        r'\.deck-stage\[data-deck-viewport\]\[data-deck-idle="true"\] \.deck-stage-surface,\s*'
        r'\.deck-stage\[data-deck-viewport\]\[data-deck-idle="true"\] \.deck-stage-surface \* \{([^}]*)\}',
        css,
    )
    assert cursor_rule and "cursor: none;" in cursor_rule.group(1), (
        "the idle pointer hide must reach the surface and its descendants, and the surface only"
    )
    assert not re.search(r'\.deck-stage:fullscreen\[data-deck-idle="true"\],', css), (
        "`cursor: none` on the stage itself is inherited only and stops at any control with its own cursor"
    )
    for name in ("dialog.tsx", "tooltip.tsx"):
        ui = (FRONTEND_ROOT / "src" / "components" / "ui" / name).read_text(encoding="utf-8")
        assert "container?: HTMLElement | null" in ui, f"{name} cannot portal into a fullscreen stage"
    assert "container={container}" in _read("DeckShortcutsDialog.tsx")
    # The stillness before the hide is ONE number, in a module without React,
    # so the e2e spec times the hide against the value the stage arms - never a
    # copy of it.
    assert 'import { IDLE_HIDE_MS } from "./deck-idle";' in stage
    idle_module = _read("deck-idle.ts")
    assert re.search(r"^export const IDLE_HIDE_MS = \d+;$", idle_module, re.M)
    assert not re.search(r"^import ", idle_module, re.M), "deck-idle.ts must stay dependency-free: the e2e spec imports it"
    # The e2e spec drives both modes on a 16:9 and a 16:10 screen, asserts the
    # native path was taken where the browser grants it, and reads the idle
    # delay from the stage's own module.
    spec = (FRONTEND_ROOT / "tests_e2e" / "presentation-fullscreen.spec.ts").read_text(encoding="utf-8")
    for needle in (
        "width: 1920, height: 1080",
        "width: 1920, height: 1200",
        "requestFullscreen = () => Promise.reject",
        "data-deck-viewport",
        "from '../src/components/presentation/deck-idle'",
        ".toBe('native')",
        "dataset.theme = 'xtm-one'",
        "buildCssVarsBlock('#ff5500', null)",
        "getByRole('menu', { name: 'More actions' })",
    ):
        assert needle in spec, f"presentation-fullscreen.spec.ts no longer covers `{needle}`"
    assert not re.search(r"\b2500\b", spec), "the e2e spec must read IDLE_HIDE_MS, never spell the delay out"


def test_frontend_present_falls_back_to_the_viewport_when_the_browser_refuses_fullscreen():
    """`Present` must always present something. The Fullscreen API can report
    itself available and then REFUSE the request (an Electron webview, a kiosk
    policy: "Permissions check failed"); the stage then takes the app's viewport
    through the browser's top layer (a manual popover — above every z-index,
    clear of a hosting dialog's transform), under the same overlay toolbar and
    the same `Esc`. A key handler racing the re-render reads the mode live from
    the DOM, as `document.fullscreenElement` is read for the native one."""
    hook = _read("fullscreen.ts")
    assert 'export type FullscreenMode = "native" | "viewport"' in hook
    assert 'VIEWPORT_FULLSCREEN_ATTR = "data-deck-viewport"' in hook
    assert "request.catch(() => enterViewport())" in hook, "a refused requestFullscreen no longer falls back"
    assert 'element.setAttribute("popover", "manual")' in hook and "element.showPopover()" in hook
    assert "element.hidePopover()" in hook
    assert "export function presentedElement()" in hook
    assert "document.fullscreenElement ?? document.querySelector" in hook
    # Escape leaves either mode — on the stage and in the dialogs that host a deck.
    assert "presentedElement() === stageRef.current" in _read("DeckStage.tsx")
    assert "overview || presentedElement()" in _read("deck-keys.ts")
    assert "data-deck-fullscreen={fullscreen.mode ?? undefined}" in _read("DeckStage.tsx")
    # Every fullscreen rule is written for both modes.
    css = _read("presentation.css")
    for native, viewport in (
        (".deck-stage:fullscreen,", ".deck-stage[data-deck-viewport] {"),
        (".deck-stage:fullscreen .deck-stage-surface,", ".deck-stage[data-deck-viewport] .deck-stage-surface {"),
        (".deck-stage:fullscreen .deck-toolbar,", ".deck-stage[data-deck-viewport] .deck-toolbar {"),
        (
            '.deck-stage:fullscreen[data-deck-idle="true"] .deck-toolbar:not(:hover):not(:has(:focus-visible)):not(:has([aria-expanded="true"])),',
            '.deck-stage[data-deck-viewport][data-deck-idle="true"] .deck-toolbar:not(:hover):not(:has(:focus-visible)):not(:has([aria-expanded="true"])) {',
        ),
        (
            '.deck-stage:fullscreen[data-deck-idle="true"] .deck-stage-surface *,',
            '.deck-stage[data-deck-viewport][data-deck-idle="true"] .deck-stage-surface * {',
        ),
    ):
        assert native in css and viewport in css, f"{viewport} is not paired with {native}"
    # Every presenting rule is written for both modes: no `:fullscreen` rule
    # without its `[data-deck-viewport]` twin. (Not the other way round: the
    # viewport mode needs a positioning rule of its own where the browser's
    # fullscreen styles do the same for the native one.)
    natives = {s.strip() for s in re.findall(r"^\.deck-stage:fullscreen([^,{\n]*)[,{]", css, re.M)}
    viewports = {s.strip() for s in re.findall(r"^\.deck-stage\[data-deck-viewport\]([^,{\n]*)[,{]", css, re.M)}
    assert len(natives) >= 6, natives
    assert natives <= viewports, f"presenting rules without a viewport twin: {sorted(natives - viewports)}"
    assert re.search(r"\.deck-stage\[data-deck-viewport\] \{[^}]*position: fixed;[^}]*inset: 0;", css)
    # The toolbar offers Present unless the API is unavailable AND a viewer tab
    # exists to present from (the embedded chat inside an iframe).
    toolbar = _read("DeckToolbar.tsx")
    assert "const presentHere = !!onPresent || fullscreen.supported || !viewerUrl;" in toolbar


def test_frontend_stage_letterboxes_the_deck_in_the_app_background():
    """The stage letterboxes the deck at rest as it does while presenting:
    the surface is a size container painted with the
    letterbox and the deck root is the largest box of the deck's own ratio
    that fits it, centred - so the deck's paper never shows outside the
    slide. A light deck whose cover carries its own dark theme sat
    between two white bands in the viewer, the studio and the chat card while
    everything around was the app's dark background. The letterbox is one
    custom property: the app's own background at rest (the app mode, the
    product theme and an administrator's branded background all come through
    the token), black while presenting."""
    from app.services.presentation_spec import PRESENTATION_ASPECT_RATIOS

    css = _read("presentation.css")
    stage_rule = re.search(r"^\.deck-stage \{([^}]*)\}", css, re.M)
    assert stage_rule, "the stage rule is gone"
    assert "--deck-stage-bg: var(--bg-elevation-default);" in stage_rule.group(1), (
        "at rest the letterbox is the app's own background, never the deck's paper"
    )
    presenting = re.search(r"\.deck-stage:fullscreen,\s*\.deck-stage\[data-deck-viewport\] \{([^}]*)\}", css)
    assert presenting and "--deck-stage-bg: #000;" in presenting.group(1), "presenting keeps its black"
    surface_rule = re.search(r"^\.deck-stage-surface \{([^}]*)\}", css, re.M)
    assert surface_rule, "the stage surface rule is gone"
    for declaration in ("display: grid;", "background: var(--deck-stage-bg);", "container-type: size;"):
        assert declaration in surface_rule.group(1), f"the stage surface lost `{declaration}`"
    # Centred by the ITEM: `place-items` on the grid would reach the slide
    # editor's layer too, an absolutely positioned child of the surface that
    # must keep covering all of it.
    assert "place-items" not in surface_rule.group(1)
    assert re.search(r"^\.deck-stage-surface > \.deck-root \{\s*place-self: center;\s*\}", css, re.M), (
        "the deck root is no longer centred in the surface"
    )
    # One rule per format of the catalog, the same at rest and while presenting.
    formats = (
        ("16:9", "16 / 9", "9 / 16"),
        ("4:3", "4 / 3", "3 / 4"),
        ("A4", "1920 / 2715", "2715 / 1920"),
        ("letter", "1920 / 2485", "2485 / 1920"),
    )
    assert {aspect for aspect, _, _ in formats} == set(PRESENTATION_ASPECT_RATIOS)
    for aspect, w, h in formats:
        root_rule = re.search(
            r"^\.deck-stage-surface\[data-aspect=\"" + re.escape(aspect) + r"\"\] > \.deck-root \{([^}]*)\}",
            css,
            re.M,
        )
        assert root_rule, f"the deck-root rule for {aspect} is gone"
        assert f"width: min(100cqw, calc(100cqh * {w}));" in root_rule.group(1), aspect
        assert f"height: min(100cqh, calc(100cqw * {h}));" in root_rule.group(1), aspect
    assert not re.search(r":fullscreen[^{]*\.deck-stage-surface\[data-aspect=", css), (
        "the deck root is sized by the stage's own rules, not by presenting ones"
    )
    # The chat card leaves the surface's paint to the stage.
    assert 'surfaceClassName="deck-aspect-box max-h-[420px] w-full"' in _read("PresentationCard.tsx")
    # A real render proves the bands: a light deck with a dark cover, the
    # pixels around the slide read in both app modes, beside the studio rail,
    # the editor on the stage.
    spec = (FRONTEND_ROOT / "tests_e2e" / "presentation-stage-letterbox.spec.ts").read_text(encoding="utf-8")
    for needle in (
        "theme: 'light'",
        "theme: 'dark'",
        "dataset.mode = 'light'",
        "?studio=1",
        "presentation.studio.rail",
        "?panel=editor",
        "presentation.editLayer",
        "presentation.blockBar",
        "pixelAt(",
    ):
        assert needle in spec, f"presentation-stage-letterbox.spec.ts no longer covers `{needle}`"


def test_frontend_the_paper_under_a_slide_is_the_slides_own():
    """The paper under a slide is that slide's own. With one deck-wide
    paper painted by the boxes that clip the deck - snapped to whole device
    pixels while a slide, scaled by reveal's transform, ends between two - the
    deck's white showed as a hairline along one edge of a dark cover on a
    light deck, which edge depending on the size of the stage (and a dark line
    along a light slide of a dark deck). Each slide now hands reveal its own
    paper as its background, painted inside the clip; on a stage the root and
    reveal's viewport paint none once reveal is ready; and the papers cover
    the deck through every slide change: every background stays laid out on a
    stage (reveal hides a far slide's background at once, which uncovered the
    stage on a jump across the deck), the papers slide with a sliding deck and
    cross-fade under the others with the outgoing one held (reveal's own
    cross-fade lets the stage show through mid-way, sliding papers swept a
    seam across a fading slide), and switch at once when the deck has no
    transition."""
    theme = _read("theme.ts")
    css = _read("presentation.css")
    # The paper of each scheme is a token declared once, and every token block
    # reads its `--deck-bg` from it - so nothing mirrors the colour.
    tokens = re.search(
        r"^\.deck-root,\s*\.deck-edit-layer,\s*\.deck-palette \{\s*"
        r"--deck-paper-dark: ([^;]+);\s*--deck-paper-light: ([^;]+);\s*\}",
        css,
        re.M,
    )
    assert tokens, "presentation.css no longer declares the paper of each scheme once"
    # The values are the design system's primitives, named rather than copied:
    # its darkest grey-blue (the dark theme's page surface) and its white. Each
    # scheme's paper is fixed - a dark slide stays dark in the light app mode -
    # so a theme-reactive alias, or the two swapped, is a regression.
    assert [tokens.group(1).strip(), tokens.group(2).strip()] == [
        "var(--grayblue-1000)",
        "var(--white-1000)",
    ]
    for scheme in ("dark", "light"):
        block = re.search(rf'\.deck-root \[data-scheme="{scheme}"\] \{{[^}}]*?--deck-bg: ([^;]+);', css)
        assert block, scheme
        assert block.group(1).strip() == f"var(--deck-paper-{scheme})", f"the {scheme} token block no longer reads its paper"
    assert len(re.findall(r"^\s*--deck-bg:", css, re.M)) == 2, "a third `--deck-bg` would drift from the paper tokens"
    papers = re.search(r"export const SCHEME_PAPER[^=]*=\s*\{([^}]*)\}", theme)
    assert papers, "theme.ts no longer declares SCHEME_PAPER"
    for scheme in ("dark", "light"):
        assert f'{scheme}: "var(--deck-paper-{scheme})",' in papers.group(1), f"SCHEME_PAPER.{scheme} is not its token"
    # A palette gradient rides reveal's gradient layer over the scheme's paper:
    # as a background colour it was dropped and the slide stood transparent.
    assert "if (GRADIENT_PAPER.test(own)) return { color: scheme, gradient: own };" in theme
    # Neither a colour nor a gradient: reveal drops it, so the scheme's paper.
    assert "return isPaperColor(own) ? { color: own } : { color: scheme };" in theme
    assert 'CSS.supports("color", value)' in theme
    # ...which also accepts the CSS-wide keywords, none of them an opaque paper.
    assert "if (CSS_WIDE_KEYWORD.test(value)) return false;" in theme
    deck = _read("PresentationDeck.tsx")
    assert "const paper = slidePaper(resolution);" in deck, "a slide no longer hands reveal its own paper"
    assert "backgroundColor={paper.color}" in deck
    # reveal's per-slide sync writes a gradient but never clears one: a slide
    # without one says `none`, or a draft that drops it keeps the old gradient.
    assert 'backgroundGradient={paper.gradient ?? "none"}' in deck, "a gradient a slide dropped would stay painted"
    assert "backgroundTransition: paperTransition(spec.transition)," in deck
    mapping = re.search(r"function paperTransition\([^)]*\)[^{]*\{(.*?)\n\}", deck, re.S)
    assert mapping, "paperTransition is gone"
    body = mapping.group(1)
    assert re.search(r'case "none":\s*return "none";', body)
    assert re.search(r'case "fade":\s*case "zoom":\s*case "convex":\s*case "concave":\s*return "fade";', body)
    assert re.search(r'default:\s*return "slide";', body)
    # On a stage, once reveal is ready, the boxes that clip paint no paper.
    assert re.search(
        r"^\.deck-stage-surface > \.deck-root:has\(> \.reveal\.ready\),\s*"
        r"\.deck-stage-surface > \.deck-root > \.reveal\.ready \{\s*background: transparent;\s*\}",
        css,
        re.M,
    ), "the deck root or reveal's viewport paints a paper again on a stage"
    # Every background stays laid out on a stage; reveal's visibility decides
    # what paints. Scoped to `.reveal > .backgrounds`: the overview moves the
    # layer into `.slides` and keeps reveal's own culling.
    assert re.search(
        r"^\.deck-stage-surface > \.deck-root > \.reveal > \.backgrounds > \.slide-background \{\s*"
        r"display: block !important;\s*\}",
        css,
        re.M,
    ), "reveal may hide the outgoing paper of a jump again"
    # A paper cross-fade holds the outgoing paper for exactly the incoming one's fade.
    fade = r'\.deck-stage-surface > \.deck-root > \.reveal\[data-background-transition="fade"\] > \.backgrounds > \.slide-background'
    duration = re.search(rf"^{fade} \{{\s*transition-duration: ([\d.]+s);\s*\}}", css, re.M)
    delay = re.search(rf"^{fade}:not\(\.present\) \{{\s*transition-delay: ([\d.]+s);\s*\}}", css, re.M)
    assert duration and delay, "the outgoing paper of a cross-fade is no longer held"
    assert duration.group(1) == delay.group(1), "the hold must last exactly the incoming paper's fade"
    # A real render reads the edges at a fractional device pixel ratio across
    # sizes, for a line lighter or darker than both sides, in both directions,
    # checks every slide's paper - colour and gradient layer, laid out, the far
    # ones included - that a deck theme picked in the editor repaints them
    # before the save, reads frozen frames of a jump and of a fade, and a
    # gradient template's paper, and that a gradient palette a draft takes
    # away (a theme, no template) leaves no gradient behind - on a draft
    # already dirty, since the first dirty edit re-creates every background.
    spec = (FRONTEND_ROOT / "tests_e2e" / "presentation-stage-letterbox.spec.ts").read_text(encoding="utf-8")
    for needle in (
        "deviceScaleFactor: 1.25",
        "bandedEdges(",
        "beyondEnds(",
        "REVERSE_SPEC",
        ".backgrounds > .slide-background",
        "display === 'block'",
        "paintedImage === ownImage",
        "createTwoSchemeTemplate(",
        "getByRole('radiogroup', { name: 'Theme', exact: true })",
        "freezeSlideChange(page, () => page.keyboard.press('End'), 150)",
        "freezeSlideChange(page, () => page.keyboard.press('Home'), 150)",
        "JUMP_BACK_SPEC",
        "FADE_SPEC",
        "['ArrowLeft', 1, 0]",
        "GRADIENT_SPEC",
        "background: 'linear-gradient(",
        "getByRole('switch', { name: 'Show slide numbers', exact: true })",
        "'No template - plain look'",
    ):
        assert needle in spec, f"presentation-stage-letterbox.spec.ts no longer covers `{needle}`"


def test_frontend_pages_are_read_as_documents_not_presented_as_decks():
    """A portrait presentation is a document, not a slide show that happens to
    be tall. The frontend derives the ``kind`` exactly as the backend
    does (the paper decides, nothing is chosen separately), and every surface
    that mounts an interactive presentation picks the reader for a page and the
    stage for a deck — so a brief never opens with a slide counter, fullscreen
    and transitions."""
    from app.services.presentation_spec import (
        DOCUMENT_ASPECT_RATIOS,
        PRESENTATION_ASPECT_RATIOS,
        PRESENTATION_KINDS,
    )

    geometry = _read("geometry.ts")
    # Same catalog, same page family, same derivation.
    assert set(re.findall(r'^\s+"([^"]+)": \{ width', geometry, re.M)) == set(PRESENTATION_ASPECT_RATIOS)
    match = re.search(r"DOCUMENT_ASPECT_RATIOS[^=]*=\s*\[([^\]]*)\]", geometry)
    assert match, "geometry.ts no longer declares DOCUMENT_ASPECT_RATIOS"
    assert set(re.findall(r'"([^"]+)"', match.group(1))) == set(DOCUMENT_ASPECT_RATIOS)
    assert 'isDocumentFormat(aspectRatio) ? "page" : "deck"' in geometry
    types = (FRONTEND_ROOT / "src" / "types" / "index.ts").read_text(encoding="utf-8")
    kinds = re.search(r"export type PresentationKind = ([^;]+);", types)
    assert kinds and set(re.findall(r'"([^"]+)"', kinds.group(1))) == set(PRESENTATION_KINDS)
    assert re.search(r"^\s+kind: PresentationKind;", types, re.M), "Presentation rows do not carry `kind`"

    # Every host of an interactive presentation branches on the kind.
    for name, path in (
        ("PresentationCard.tsx", PRESENTATION_DIR / "PresentationCard.tsx"),
        ("PreviewDeck.tsx", PRESENTATION_DIR / "PreviewDeck.tsx"),
        ("PresentationViewer.tsx", FRONTEND_ROOT / "src" / "pages" / "PresentationViewer.tsx"),
    ):
        text = path.read_text(encoding="utf-8")
        assert 'kind === "page"' in text, f"{name} does not tell a page from a deck"
        assert "PageDocument" in text and "DeckStage" in text, f"{name} does not pick the surface by kind"

    # The reader is a document: scrolled sheets and zoom, nothing of a stage.
    document = _read("PageDocument.tsx")
    for forbidden in (
        "useElementFullscreen",
        "requestFullscreen",
        'from "reveal.js"',
        'from "./PresentationDeck"',
        "<DeckStage",
        "DeckToolbar",
    ):
        assert forbidden not in document, f"PageDocument carries a slide-show feature: {forbidden}"
    assert "StaticSlide" in document, "PageDocument does not render the export's own canvas"
    assert 'role="document"' in document
    controller = _read("page-controller.ts")
    assert "PAGE_ZOOM_STEPS" in controller and "READING_LINE" in controller
    css = _read("presentation.css")
    for rule in (".page-surface", ".page-sheet", "--page-scale"):
        assert rule in css, f"presentation.css has no rule for {rule}"
    # And the words follow the kind: the sheet, the jump menu and the editor
    # say "page", and the user docs describe the reader.
    assert 'kind === "page"' in _read("DeckShortcutsDialog.tsx")
    assert 'kind === "page"' in _read("SlideJumpMenu.tsx")
    assert 'kind === "page"' in _read("PresentationEditorPanel.tsx")
    docs = (BACKEND_ROOT.parent / "docs" / "docs" / "usage" / "presentations.md").read_text(encoding="utf-8")
    assert "## Read a page" in docs
    for doc_key in ("++plus++", "++minus++", "++0++"):
        assert doc_key in docs, f"usage docs do not list the reader's {doc_key}"


# ── Reference fixtures ──────────────────────────────────────────────────────

FIXTURE_DIR = BACKEND_ROOT.parent / "dev-docs" / "presentation-template-package"

#: The blocks the sales-content pass added. The two collateral fixtures
#: are the reference reproductions of the marketing collateral: between them
#: they must exercise every one of these, on a slide AND on a portrait page.
COLLATERAL_BLOCKS = ("badges", "key_value", "pricing_tiers", "banner", "personas")


def _fixture(name: str) -> dict:
    import json

    if not FIXTURE_DIR.is_dir():  # pragma: no cover - fixture removed
        pytest.skip("dev-docs fixture not present in this checkout")
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _block_types(node: object) -> set[str]:
    """Every ``type`` reachable in a spec, containers included."""
    found: set[str] = set()
    if isinstance(node, dict):
        block_type = node.get("type")
        if isinstance(block_type, str):
            found.add(block_type)
        for value in node.values():
            found |= _block_types(value)
    elif isinstance(node, list):
        for item in node:
            found |= _block_types(item)
    return found


def test_every_reference_deck_fixture_still_validates():
    """``dev-docs/presentation-template-package/deck-*.json`` are what the
    README, the loader script and the collateral review were run against. A
    spec change that silently invalidates one turns the documented fixture
    into a lie; ``deck-invalid.json`` must keep failing for the same reason."""
    if not FIXTURE_DIR.is_dir():  # pragma: no cover - fixture removed
        pytest.skip("dev-docs fixture not present in this checkout")
    decks = sorted(path.name for path in FIXTURE_DIR.glob("deck-*.json"))
    assert "deck-invalid.json" in decks and len(decks) >= 4
    for name in decks:
        payload = _fixture(name)
        if name == "deck-invalid.json":
            with pytest.raises(PresentationSpecError):
                validate_spec(payload)
            continue
        spec = validate_spec(payload)
        assert spec["slides"], name


def test_collateral_fixtures_cover_every_collateral_block_on_both_formats():
    brief = _fixture("deck-product-brief.json")
    deck = _fixture("deck-sales-collateral.json")
    # A portrait page and a slide deck - the two shapes the collateral takes.
    assert brief["aspect_ratio"] in ("A4", "letter")
    assert deck.get("aspect_ratio", "16:9") == "16:9"
    for payload, name in ((brief, "brief"), (deck, "deck")):
        missing = set(COLLATERAL_BLOCKS) - _block_types(payload)
        assert not missing, f"the {name} fixture no longer exercises {sorted(missing)}"
    # The brief is the 1-2 page shape the skill prescribes, not a deck in
    # portrait clothes.
    assert 1 <= len(brief["slides"]) <= 2


# ── Per-slide themes, heroes and overlays ────────────────────────────


def _cover(**fields) -> dict:
    return {"layout": "title", "title": "Uncover Threats.\nTake Action.", **fields}


def test_slide_theme_defaults_to_inherit_and_never_lands_on_a_stored_spec():
    """A stored spec without a slide ``theme`` validates unchanged: no slide gains a
    ``theme`` key (``None`` is dropped), every slide reads in the deck's
    scheme, and the new look fields materialise their defaults like every
    other default (``hero: plain``, ``overlay: none``)."""
    spec = validate_spec(_deck(_cover(), {"layout": "closing"}, theme="light"))
    for slide in spec["slides"]:
        assert "theme" not in slide
        assert slide["hero"] == "plain"
        assert slide["hero_tone"] == "brand"
        assert slide["overlay"] == "none"
        assert resolved_slide_scheme(spec, slide) == "light"
    assert slide_theme_overrides(spec) == []
    assert spec["slides"][0]["hero_accent_word"] is False


def test_slide_theme_wins_over_the_deck_theme_and_is_normalised_like_it():
    spec = validate_spec(
        _deck(
            _cover(theme="dark", hero="gradient", hero_accent_word=True),
            {"layout": "content", "title": "Body", "blocks": [{"type": "text", "content": "x"}]},
            {"layout": "closing", "theme": " Light ", "hero": "spotlight"},
            theme="light",
        )
    )
    cover, body, closing = spec["slides"]
    assert cover["theme"] == "dark" and cover["hero"] == "gradient" and cover["hero_accent_word"] is True
    # The hero's colour is a palette choice, kept as written; absent = the brand gradient.
    assert cover["hero_tone"] == "brand" and closing["hero_tone"] == "brand"
    toned = validate_spec(_deck(_cover(hero="gradient", hero_tone="accent"), {"layout": "closing", "hero_tone": "gradient_from"}))
    assert toned["slides"][0]["hero_tone"] == "accent" and toned["slides"][1]["hero_tone"] == "gradient_from"
    assert "theme" not in body
    # ``" Light "`` folds like the deck theme; the retired ``filigran`` reads as dark.
    assert closing["theme"] == "light"
    assert resolved_slide_scheme(spec, cover) == "dark"
    assert resolved_slide_scheme(spec, body) == "light"
    assert slide_theme_overrides(spec) == [(0, "dark"), (2, "light")]
    legacy = validate_spec(_deck(_cover(theme="filigran")))
    assert legacy["slides"][0]["theme"] == "dark"
    # A blank theme is "inherit", like every blank optional string.
    blank = validate_spec(_deck(_cover(theme="")))
    assert "theme" not in blank["slides"][0]


@pytest.mark.parametrize(
    ("field", "value", "layout"),
    [
        ("theme", "blue", "title"),
        ("theme", "sepia", "content"),
        ("hero", "neon", "title"),
        ("hero", "gradient", "content"),
        ("hero_tone", "cyan", "title"),
        ("hero_tone", "#0FBCFF", "closing"),
        ("hero_tone", "accent", "content"),
        ("overlay", "heavy", "title"),
        ("hero_accent_word", "maybe", "title"),
        ("hero_accent_word", True, "closing"),
    ],
)
def test_unknown_slide_look_values_are_refused_by_slide_and_field(field, value, layout):
    slide: dict = {"layout": layout, "title": "T", field: value}
    if layout == "content":
        slide["blocks"] = [{"type": "text", "content": "x"}]
    with pytest.raises(PresentationSpecError) as exc_info:
        validate_spec(_deck({"layout": "section", "title": "S"}, slide))
    message = str(exc_info.value)
    assert "slides.1" in message and f"layout={layout}" in message
    assert field in message


def test_hero_and_overlay_catalogs_are_the_models_and_the_reference_carries_them():
    """The frontend mirrors (`hero.ts`) and the tool description are pinned to
    the Literals: a variant added on one side only would render a blank
    backdrop or be refused by the validator."""
    assert HERO_VARIANTS == ("plain", "gradient", "split", "spotlight", "mosaic", "cascade", "horizon", "pillar")
    assert HERO_TONES == ("brand", "accent", "gradient_from", "gradient_to", "info", "success", "warning", "error", "muted")
    assert OVERLAY_VARIANTS == ("none", "soft", "strong")
    hero_ts = _read("hero.ts")
    variants_ts = hero_ts[hero_ts.index("export const HERO_VARIANTS") : hero_ts.index("];", hero_ts.index("export const HERO_VARIANTS"))]
    assert re.findall(r'"(\w+)"', variants_ts) == list(HERO_VARIANTS), "hero.ts lists the heroes in another order"
    # The covers every deck may wear; the others are a template's own.
    from app.services.presentation_spec import SHARED_HEROES, TEMPLATE_HEROES

    assert SHARED_HEROES + TEMPLATE_HEROES == HERO_VARIANTS
    shared_ts = hero_ts[hero_ts.index("export const SHARED_HEROES") : hero_ts.index(";", hero_ts.index("export const SHARED_HEROES"))]
    assert re.findall(r'"(\w+)"', shared_ts) == list(SHARED_HEROES), "hero.ts shares other covers than the backend"
    assert 'export const OVERLAY_VARIANTS: readonly DeckOverlay[] = ["none", "soft", "strong"];' in hero_ts
    tones_ts = hero_ts[hero_ts.index("export const HERO_TONES") : hero_ts.index("];", hero_ts.index("export const HERO_TONES"))]
    assert [tone for tone in HERO_TONES if f'"{tone}"' not in tones_ts] == []
    assert tones_ts.count('"') == 2 * len(HERO_TONES), "hero.ts lists a tone the backend does not"
    reference = spec_reference()
    shared = next(line for line in reference.splitlines() if line.startswith("SLIDE LAYOUTS"))
    # The precedence rule in one sentence, where every layout is described.
    assert "theme?: SlideTheme" in shared
    assert "always wins over the deck theme" in shared
    assert 'overlay?: Overlay = "none"' in shared
    title = next(line for line in reference.splitlines() if line.startswith("- title:"))
    assert 'hero?: Hero = "plain"' in title and "hero_accent_word?: bool = false" in title
    assert 'hero_tone?: HeroTone = "brand"' in title
    closing = next(line for line in reference.splitlines() if line.startswith("- closing:"))
    assert 'hero?: Hero = "plain"' in closing and 'hero_tone?: HeroTone = "brand"' in closing
    assert '- SlideTheme: "dark"|"light"' in reference
    assert '- Hero: "plain"|"gradient"|"split"|"spotlight"|"mosaic"|"cascade"|"horizon"|"pillar"' in reference
    assert '- HeroTone: "brand"|"accent"|"gradient_from"|"gradient_to"|"info"|"success"|"warning"|"error"|"muted"' in reference
    assert '- Overlay: "none"|"soft"|"strong"' in reference
    # The layout catalog (the template `layouts` guardrail) lists the fields too.
    catalog = layout_catalog()
    assert {"theme", "overlay", "hero", "hero_tone", "hero_accent_word"} <= set(catalog["title"])
    assert {"theme", "overlay", "hero", "hero_tone"} <= set(catalog["closing"])
    assert "hero" not in catalog["content"] and "hero_tone" not in catalog["content"] and "theme" in catalog["content"]


def test_a_title_slide_names_up_to_four_speakers_and_reads_the_one_speaker_form_as_the_lead():
    """`speakers` lists who presents, each with a portrait. The
    one-speaker form a deck was stored with (`speaker` + `speaker_photo_file_id`)
    is the lead speaker on every write and on every read of the column, so no
    reader meets two shapes; a portrait with no speaker to wear it is dropped."""
    from app.services.presentation_legacy import fold_title_speakers

    def title(**fields):
        return validate_spec({"title": "T", "slides": [{"layout": "title", "title": "Cover", **fields}]})["slides"][0]

    four = [{"name": f"Speaker {n}", "photo_file_id": f"photo-{n}"} for n in range(1, 5)]
    assert [s["name"] for s in title(speakers=four)["speakers"]] == ["Speaker 1", "Speaker 2", "Speaker 3", "Speaker 4"]
    with pytest.raises(PresentationSpecError, match="speakers"):
        title(speakers=[*four, {"name": "Speaker 5"}])
    legacy = title(speaker={"name": "Ada", "title": "CTO"}, speaker_photo_file_id="photo-ada")
    assert legacy["speakers"] == [{"name": "Ada", "title": "CTO", "credentials": [], "photo_file_id": "photo-ada"}]
    assert "speaker" not in legacy and "speaker_photo_file_id" not in legacy
    # `speaker` on a slide that lists speakers replaces the lead and keeps its portrait.
    lead = title(speakers=four[:2], speaker={"name": "New lead"})["speakers"]
    assert [(s["name"], s.get("photo_file_id")) for s in lead] == [("New lead", "photo-1"), ("Speaker 2", "photo-2")]
    assert "speakers" not in title(speaker_photo_file_id="orphan")
    # The closing slide keeps its one speaker.
    closing = validate_spec({"title": "T", "slides": [{"layout": "closing", "speaker": {"name": "Ada"}}]})["slides"][0]
    assert closing["speaker"]["name"] == "Ada"
    # The read side: a stored deck in the old form reads folded, and a deck
    # with nothing to fold comes back as the same object.
    stored = {"title": "T", "slides": [{"layout": "title", "title": "Cover", "speaker": {"name": "Ada"}}]}
    assert fold_title_speakers(stored)["slides"][0]["speakers"] == [{"name": "Ada"}]
    current = {"title": "T", "slides": [{"layout": "title", "title": "Cover", "speakers": [{"name": "Ada"}]}]}
    assert fold_title_speakers(current) is current


def test_frontend_renders_every_hero_variant_and_the_opening_layouts_publish_it():
    """`data-hero` on `.deck-title` / `.deck-closing`, the backdrop component
    for every non-plain variant, the stylesheet's token-only rules, and the
    isolation state the PPTX capture relies on."""
    opening = _read("layouts", "opening.tsx")
    assert 'data-hero={hero}' in opening
    assert opening.count("<HeroBackdrop") == 2
    # Both openings hand the backdrop the slide's colour choice.
    assert opening.count("tone={slideHeroTone(slide)}") == 2
    assert "accentLastLine={slide.hero_accent_word === true}" in opening
    backdrop = _read("HeroBackdrop.tsx")
    # The tiled heroes share one drawing, each with its own pattern;
    # every other variant draws a backdrop of its own.
    hero_ts = _read("hero.ts")
    tiled = re.findall(r"^  (\w+): \"(?:scatter|cascade|horizon|pillar)\",$", hero_ts, re.MULTILINE)
    assert tiled == ["mosaic", "cascade", "horizon", "pillar"], tiled
    for variant in HERO_VARIANTS[1:]:
        assert f'case "{variant}":' in backdrop, variant
        if variant not in tiled:
            assert f'data-hero="{variant}"' in backdrop, variant
    assert "data-hero={hero}" in backdrop and "MOSAIC_HEROES[hero]" in backdrop
    # Every non-plain backdrop publishes the colour choice the stylesheet maps.
    assert backdrop.count("data-hero-tone={tone}") == len(HERO_VARIANTS) - len(tiled)
    assert 'data-export-backdrop="true"' in backdrop
    assert 'aria-hidden="true"' in backdrop
    # No raster, no HTML sink: the arcs are inline SVG drawn by React.
    assert "<svg" in backdrop and "dangerouslySetInnerHTML" not in backdrop
    css = _read("presentation.css")
    for variant in HERO_VARIANTS[1:]:
        assert f'.deck-hero-backdrop[data-hero="{variant}"]' in css or f'[data-hero="{variant}"]' in css, variant
    # One mapping per tone (brand is the pair's default), shared with the
    # inspector's chips so a chip is painted in the colour the slide draws.
    for tone in HERO_TONES[1:]:
        assert f'.deck-root .deck-hero-backdrop[data-hero-tone="{tone}"],' in css, tone
        assert f'.deck-palette .deck-hero-tone-swatch[data-hero-tone="{tone}"]' in css, tone
    assert ".deck-palette," in css and '.deck-palette[data-deck-theme="light"],' in css
    # The token blocks are declared for the slide roots as well as the deck root.
    assert '.deck-root [data-scheme="dark"]' in css and '.deck-root [data-scheme="light"]' in css
    assert "isolation: isolate;" in css
    assert ".deck-hero-accent" in css
    assert "[data-export-isolate]" in css
    # The hero rules read deck tokens, never a hard-coded colour: the only
    # literal opacity is the arc line's.
    hero_rules = css[css.index("Backdrops under the content") : css.index("Export capture isolation")]
    hero_rules = re.sub(r"\(#\d+\)", "", hero_rules)  # issue references in comments
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", hero_rules), "hero CSS carries a hard-coded colour"
    # ... and every drawing reads the tone PAIR, never a palette token directly,
    # or a `hero_tone` would leave part of a backdrop in the brand colours.
    drawings = hero_rules[hero_rules.index("/* The chip itself") : hero_rules.index(".deck-root .deck-hero-accent")]
    assert "var(--deck-hero-from)" in drawings and "var(--deck-hero-to)" in drawings
    for token in ("--deck-accent", "--deck-gradient-from", "--deck-gradient-to", "--deck-info", "--deck-chart-1"):
        assert f"var({token})" not in drawings, token
    assert "var(--deck-hero-arc-opacity)" in hero_rules
    # The mosaic draws in six slots the tone section fills: the chart series
    # for `brand`, steps of the pair for every other tone - so a single-colour
    # tone recolours every tile, and the drawing reads the slots only.
    mapping = hero_rules[: hero_rules.index("/* The chip itself")]
    brand = mapping[mapping.index('.deck-root .deck-hero-backdrop[data-hero-tone="brand"] {') :]
    for slot in range(1, 7):
        assert f"--deck-hero-mosaic-{slot}: var(--deck-chart-{slot});" in brand, slot
        assert f'rect[data-slot="{slot}"] {{ fill: var(--deck-hero-mosaic-{slot}); }}' in drawings, slot
    assert "--deck-hero-mosaic-1: var(--deck-hero-from);" in mapping
    assert "--deck-hero-mosaic-6: var(--deck-hero-to);" in mapping
    # A brand mosaic is the template's colour set exactly: a tinted
    # tile is a shade only on a single-colour tone, never on `brand`.
    tints = re.findall(r"^(.*)rect\[data-slot=\"\d\"\]\[data-tint\]", drawings, re.MULTILINE)
    assert len(tints) == 6
    assert all(':not([data-hero-tone="brand"])' in selector for selector in tints), tints
    # The tiles come from the pure composition module, seeded by the slide.
    assert "mosaicTiles(" in backdrop and "useDeckCanvas()" in backdrop
    assert opening.count("seed={slide.id ??") == 2


def test_every_slide_root_resolves_its_scheme_through_the_one_resolver():
    """`resolveSlideScheme` is the ONE reader of a slide's scheme: the
    static canvas behind the export, the PDF and the layout check, and the
    reveal section on screen both publish what it resolved, and the chrome
    draws the logo variant of THAT scheme."""
    theme_ts = _read("theme.ts")
    assert "export function resolveSlideScheme(" in theme_ts
    assert "export function slideRootAttributes(" in theme_ts
    static = _read("StaticSlide.tsx")
    deck = _read("PresentationDeck.tsx")
    for host in (static, deck):
        assert "resolveSlideScheme(presentation, slide)" in host
        assert 'data-scheme={root["data-scheme"]}' in host
        assert "theme={resolution.scheme}" in host
        assert "themeConfig={resolution.tokens}" in host
        assert "heroHostsBackgroundImage(slide)" in host
    # An overriding section paints its own paper and image (no flash); an
    # inheriting one keeps reveal's background layer.
    assert 'backgroundImage={!ownPaper && background ? background : undefined}' in deck
    assert 'section[data-scheme-override="true"]' in _read("presentation.css")


def test_the_panel_snaps_slide_themes_for_a_tried_template_only():
    """The editor mirrors the write rule of `introduced_slide_themes`:
    under the deck's own template a stored override the template stopped
    supporting is kept as stored (the save keeps it, the picker greys the
    scheme out), so the slide-theme snap runs for a TRIED template only - never
    on mount, never when the stored template is picked back - and it is keyed
    on the pick and its schemes, never on the slides (an effect watching the
    slides re-snapped the theme an undo had just restored) and never on the
    schemes alone (a relink between two dark-only templates is a template
    change the save snaps against, so the preview must snap as well)."""
    panel = _read("PresentationEditorPanel.tsx")
    effect = panel[panel.index("const [snappedSlides, setSnappedSlides]") : panel.index("// Hand the host the draft to render")]
    assert "if (!templateChanged) return;" in effect
    assert 'editor.writeAt(index, "theme", fitted, "theme");' in effect
    assert '}, [draft.template_container_id, supportedSchemes.join("|")]);' in effect
    assert "draft.slides," not in effect.rsplit("}, [", 1)[1]


def test_the_accent_line_is_the_last_word_cut_out_of_its_run():
    """`hero_accent_word` promises the last LINE, or the last WORD of a one-line
    title: the renderer reads the split off `splitAccentLine` (a pure module,
    unit-tested in `accent-line.test.ts`) and never accents a whole formatted
    run when only its last word was asked for."""
    markup = _read("inline-markup.tsx")
    assert 'import { splitAccentLine } from "./accent-line";' in markup
    assert "const { head, accent, trailing } = splitAccentLine(segments);" in markup
    assert 'lastIndexOf(" ")' not in markup, "the word split lives in accent-line.ts, not in the renderer"
    splitter = _read("accent-line.ts")
    assert "export function splitAccentLine(" in splitter
    assert 'if (segment.kind === "code") continue;' in splitter
    # The unit test pins the run-splitting case the renderer used to get wrong.
    assert 'split("Launch **Take Action.**")' in _read("accent-line.test.ts")


def test_the_design_editor_writes_deck_defaults_through_the_one_writer_that_keeps_the_covers():
    """`deck_defaults` holds the string deck keys the Design editor's selects
    drive AND the per-cover objects a brand declares in the JSON. Every
    write of the editor goes through `withDeckDefault` (`template-deck-defaults.ts`,
    unit-tested), which changes one string key and carries the rest through;
    the object rebuilt from the selects' strings dropped a duplicate brand
    deck's covers on its first transition change."""
    editor = _read("TemplateDesignEditor.tsx")
    assert 'import { readDeckDefaults, withDeckDefault } from "./template-deck-defaults";' in editor
    assert "onExtraMetadataChange(withDeckDefault(extraMetadata, key, value))" in editor
    assert 'return withDeckDefault(extraMetadata, "theme", supported[0]);' in editor
    assert "next.deck_defaults = nextDefaults" not in editor
    writer = _read("template-deck-defaults.ts")
    assert "export function withDeckDefault(" in writer
    assert "const nextDefaults = readDeckDefaultsRaw(extraMetadata);" in writer
    assert 'withDeckDefault(brand, "transition", "slide")' in _read("template-deck-defaults.test.ts")


def test_a_column_ratio_is_a_two_column_setting_on_every_surface():
    """The validator refuses `wide_*` on three columns, so the stylesheet draws
    three columns equal whatever `data-ratio` a draft carries (the stage never
    previews a look the save refuses) and the inspector says the save refuses
    it, never that it has no effect."""
    css = _read("presentation.css")
    assert '.deck-root .deck-columns[data-count="2"][data-ratio="wide_left"]' in css
    assert '.deck-root .deck-columns[data-count="3"][data-ratio=' not in css
    applicability = _read("field-applicability.ts")
    assert "wide left / right needs exactly two, and the save refuses it." in applicability
    assert "the ratio is for exactly two" not in applicability
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(
            _deck(
                {
                    "layout": "content",
                    "title": "Three",
                    "columns": [[{"type": "text", "content": c}] for c in ("a", "b", "c")],
                    "ratio": "wide_left",
                }
            )
        )
    assert "requires exactly 2 columns" in str(exc.value)


# ── Spec: a metrics grid's `size` ───────────────────────────────────
#
# Three identical KPI rows of one deck rendered at three sizes: the body fit
# shrank each row with the slide around it, and there was no field to ask for
# smaller tiles. `size` sets the tiles' type and padding, and the renderer
# holds a grid at its size when its body is shrunk (``fit.ts``), so one size
# reads the same on every slide.

_KPI_ROW = {
    "type": "metrics_grid",
    "columns": 4,
    "density": "tight",
    "variant": "card",
    "items": [
        {"value": "EUR 12.4M", "label": "Revenue"},
        {"value": "109%", "label": "Net retention"},
        {"value": "37", "label": "New logos"},
        {"value": "3.1 mo", "label": "Payback"},
    ],
}


@pytest.mark.parametrize("size", ["sm", "md", "lg"])
def test_a_metrics_grid_takes_a_size(size: str):
    stored = validate_spec(_deck(_content({**_KPI_ROW, "size": size})))
    assert stored["slides"][0]["blocks"][0]["size"] == size


def test_a_metrics_grid_is_md_when_unset_and_the_size_is_the_only_difference():
    plain = validate_spec(_deck(_content(dict(_KPI_ROW))))["slides"][0]["blocks"][0]
    small = validate_spec(_deck(_content({**_KPI_ROW, "size": "sm"})))["slides"][0]["blocks"][0]
    assert plain["size"] == "md"
    # A deck saved before the field reads back as one that says `size: md`.
    assert {key: value for key, value in plain.items() if key != "size"} == {
        key: value for key, value in small.items() if key != "size"
    }


@pytest.mark.parametrize("size", ["xl", "full", "small", 2, None])
def test_a_metrics_grid_refuses_a_size_off_the_block_scale(size: object):
    with pytest.raises(PresentationSpecError):
        validate_spec(_deck(_content({**_KPI_ROW, "size": size})))


def test_one_size_on_every_slide_of_a_deck_is_stored_as_written():
    """The same KPI row on three content slides."""
    slides = [_content({**_KPI_ROW, "size": "sm"}, {"type": "text", "content": f"Slide {n}"}) for n in (2, 3, 4)]
    stored = validate_spec(_deck({"layout": "title", "title": "Q3"}, *slides))
    grids = [block for slide in stored["slides"] for block in slide.get("blocks", []) if block["type"] == "metrics_grid"]
    assert [grid["size"] for grid in grids] == ["sm", "sm", "sm"]


def test_the_reference_documents_the_size_and_that_it_reads_the_same_on_every_slide():
    (line,) = [line for line in spec_reference().splitlines() if line.startswith("- metrics_grid:")]
    assert 'size?: Size = "md" (tile type + padding, the same on every slide: sm ~70% of md, lg ~125%)' in line


def test_the_guidance_keeps_a_recurring_kpi_row_at_one_size():
    from app.services import presentation_guidance as guidance

    (rule,) = [rule for rule in guidance.soft_budgets() if rule.startswith("metrics:")]
    assert "a KPI row that recurs across the deck uses one size and one density on every slide" in rule
    assert guidance.format_soft_budgets().count("one size and one density") == 1


def test_the_renderer_draws_the_size_the_spec_stores():
    """The grid root carries its size (md included, as the text and list
    blocks do) and the stylesheet scales the tiles off it: ``sm`` the value at
    70% of the density's, ``lg`` at 125%, ``md`` the density's own value -
    the look a grid had before the field. The frontend type carries it."""
    metrics = _read("blocks", "metrics.tsx")
    assert 'data-size={block.size ?? "md"}' in metrics
    css = _read("presentation.css")
    assert re.search(r'\.deck-metrics\[data-size="sm"\]\s*\{[^}]*--deck-metric-scale:\s*0\.7;', css)
    assert re.search(r'\.deck-metrics\[data-size="lg"\]\s*\{[^}]*--deck-metric-scale:\s*1\.25;', css)
    assert '.deck-metrics[data-size="md"]' not in css
    assert re.search(r"\.deck-metrics\s*\{[^}]*--deck-metric-value-fs:\s*72px;", css)
    assert re.search(r'\.deck-metrics\[data-density="tight"\]\s*\{[^}]*--deck-metric-value-fs:\s*44px;', css)
    assert ".deck-metrics .deck-metric-value { font-size: calc(var(--deck-metric-value-fs) * var(--deck-metric-k)); }" in css
    types = (FRONTEND_ROOT / "src" / "types" / "index.ts").read_text(encoding="utf-8")
    body = types.split("export interface DeckMetricsGridBlock ", 1)[1].split("\n}\n", 1)[0]
    assert "size?: DeckSize;" in body


# ── Spec: a table's `size` and `fill` ───────────────────────────────
#
# A 6 x 6 budget table on a content slide left a band of empty height above
# the footer callout, and nothing in the spec could enlarge its text or
# spread its rows: `density` only stepped the padding down. `size` steps the
# header, cell and footnote type along the deck scale and scales the padding;
# `fill` is the bounded row spread every other grid block takes.

_BUDGET_TABLE = {
    "type": "table",
    "headers": ["Metric", "Q1", "Q2", "Q3", "Q4", "Year"],
    "rows": [[f"Line {row}", *[str(row * cell) for cell in range(1, 6)]] for row in range(1, 7)],
    "footnote": "Figures in EUR M, unaudited.",
}


@pytest.mark.parametrize("size", ["sm", "md", "lg"])
def test_a_table_takes_a_size(size: str):
    stored = validate_spec(_deck(_content({**_BUDGET_TABLE, "size": size})))
    assert stored["slides"][0]["blocks"][0]["size"] == size


def test_a_table_is_md_and_packed_when_unset_and_the_fields_are_the_only_difference():
    plain = validate_spec(_deck(_content(dict(_BUDGET_TABLE))))["slides"][0]["blocks"][0]
    sized = validate_spec(_deck(_content({**_BUDGET_TABLE, "size": "lg", "fill": True})))["slides"][0]["blocks"][0]
    assert plain["size"] == "md"
    assert plain["fill"] is False
    assert sized["size"] == "lg"
    assert sized["fill"] is True
    # A deck saved before the fields reads back as one that says `size: md`,
    # `fill: false`, and nothing else moves.
    assert {key: value for key, value in plain.items() if key not in ("size", "fill")} == {
        key: value for key, value in sized.items() if key not in ("size", "fill")
    }


@pytest.mark.parametrize("size", ["xl", "full", "large", 2, None])
def test_a_table_refuses_a_size_off_the_block_scale(size: object):
    with pytest.raises(PresentationSpecError):
        validate_spec(_deck(_content({**_BUDGET_TABLE, "size": size})))


def test_the_sparse_remedy_never_stretches_a_table():
    """The table keeps its `fill` field, but the sparse remedy never names it:
    blocks are sized to their content."""
    from app.services.presentation_layout import sparse_remedy

    assert "table" in FILL_BLOCK_TYPES
    for table in (dict(_BUDGET_TABLE), {**_BUDGET_TABLE, "fill": True}):
        remedy = sparse_remedy(validate_spec(_deck(_content(table))), 0, "slide")
        assert remedy.startswith("Add content to the body")
        assert "set fill" not in remedy.lower()
        assert "distribute" not in remedy


def test_the_reference_documents_the_table_size_and_fill():
    (line,) = [line for line in spec_reference().splitlines() if line.startswith("- table:")]
    assert 'size?: Size = "md" (header, cell and footnote type one step down (sm) or up (lg), padding 80% / 125%)' in line
    assert f"fill?: bool = false ({FILL_ROWS_DESCRIPTION})" in line


def test_the_guidance_gives_a_lone_table_fill_and_the_room_its_size():
    from app.services import presentation_guidance as guidance

    (rule,) = [rule for rule in guidance.soft_budgets() if rule.startswith("tables:")]
    assert "a table that carries the slide alone takes fill, and size lg when it has the room" in rule


def test_the_renderer_draws_the_table_size_the_spec_stores():
    """The root carries its size (md included, as the list and grid blocks do)
    and the stylesheet steps the type and scales the padding off it: ``md`` is
    the look a table had before the field, in every density; ``tight`` keeps
    its one step down from the size's type. The frontend type carries both
    fields and the inspector offers them."""
    table = _read("blocks", "table.tsx")
    assert 'data-size={block.size ?? "md"}' in table
    css = _read("presentation.css")
    root = re.search(r"\.deck-root \.deck-table-block \{([^}]*)\}", css)
    assert root and "--deck-table-fs: var(--deck-fs-sm);" in root.group(1)
    assert root and "--deck-table-fs-tight: var(--deck-fs-xs);" in root.group(1)
    assert root and "--deck-table-footnote-fs: var(--deck-fs-xs);" in root.group(1)
    assert root and "--deck-table-scale: 1;" in root.group(1)
    assert re.search(r'\.deck-table-block\[data-size="sm"\]\s*\{[^}]*--deck-table-scale:\s*0\.8;', css)
    assert re.search(r'\.deck-table-block\[data-size="lg"\]\s*\{[^}]*--deck-table-scale:\s*1\.25;', css)
    assert re.search(r'\.deck-table-block\[data-size="lg"\]\s*\{[^}]*--deck-table-fs:\s*var\(--deck-fs-md\);', css)
    assert '.deck-table-block[data-size="md"]' not in css
    assert "padding: calc(18px * var(--deck-table-scale)) calc(28px * var(--deck-table-scale));" in css
    assert ".deck-root .deck-table-footnote {\n  flex: none;\n  padding: 12px 4px 0;\n  font-size: var(--deck-table-footnote-fs);" in css
    types = (FRONTEND_ROOT / "src" / "types" / "index.ts").read_text(encoding="utf-8")
    body = types.split("export interface DeckTableBlock ", 1)[1].split("\n}\n", 1)[0]
    assert "size?: DeckSize;" in body and "fill?: boolean;" in body
    schema = _read("block-schema.ts")
    table_entry = schema.split("  table: {", 1)[1].split("\n  },\n", 1)[0]
    assert 'en("size", "Size", SIZES)' in table_entry
    assert 'flag("fill", "Fill the height"' in table_entry


def test_a_fill_table_spreads_its_body_rows_bounded_in_a_run_and_to_the_panel_floor():
    """The table's spread grows its ROWS, not gaps: the stretched ``<table>``
    hands its extra height to the body rows (the header row is constrained,
    so it keeps its height), each capped by ``spread.ts`` through a measurer
    of its own that reads the natural ``<tr>``s. In a dashboard panel the
    same rules spread the body rows to the panel's floor (#10228), uncapped:
    the measurer only bounds a block of a run. A ``fill`` table in a stretched
    or equal-height column is that column's room, uncapped too (#10306)."""
    css = _read("presentation.css")
    hook = '.deck-root :is(.deck-blocks, .deck-panel-body) > .deck-table-block[data-fill="true"]'
    assert f"{hook} .deck-table-wrap {{\n  display: flex;\n  flex-direction: column;\n}}" in css
    assert f"{hook} .deck-table {{ flex: 1 1 auto; }}" in css
    assert f"{hook} .deck-table thead tr {{ height: 1px; }}" in css
    assert f"{hook} .deck-table td {{ vertical-align: middle; }}" in css
    assert '.deck-root .deck-table-block[data-fill="true"] .deck-table' not in css.replace(hook, "")
    spread = _read("spread.ts")
    assert "export function measureTableRows(el: HTMLElement): SpreadMeasurement | null {" in spread
    assert (
        'if (!el.matches(".deck-blocks > *") || inStretchedColumn(el)) return { rows: [], gap: 0, frame: 0, filler: true };'
        in spread
    )
    assert (
        '\'.deck-columns:is([data-valign="stretch"], [data-equal-height="true"]) > .deck-column > .deck-blocks > *\''
        in spread
    )
    assert "const rows = [...table.rows].map((row) => row.offsetHeight).filter((height) => height > 0);" in spread
    table = _read("blocks", "table.tsx")
    assert "useSpreadCap(rootRef, block.fill, SPREAD_ROW_MAX_EXTRA_PX, block.rows, measureTableRows)" in table


# ── The tone of a KPI delta ─────────────────────────────────────────
#
# The Q3 2026 quarterly review: a loss, a burn and a burn multiple that fell are
# good news, and a delta was coloured by its direction alone - a red down arrow
# on each. `delta_tone` names the news; the arrow keeps the trend's direction.

_GOOD_FALLS = [
    {"value": "EUR 1.7M", "label": "Loss, Q3", "delta": "-18.4% QoQ", "trend": "down", "delta_tone": "success"},
    {"value": "EUR 3.1M", "label": "Burn, Q3", "delta": "-12.5% QoQ", "trend": "down", "delta_tone": "success"},
    {"value": "1.27x", "label": "Burn multiple", "delta": "-0.20x", "trend": "down", "delta_tone": "success"},
]


def test_a_kpi_tile_takes_a_delta_tone_wherever_a_tile_is_drawn():
    stored = validate_spec(
        _deck(
            _content({"type": "metric", **_GOOD_FALLS[0]}, {"type": "metrics_grid", "items": _GOOD_FALLS}),
            _dashboard(tiles=_GOOD_FALLS, panels=[{"block": {"type": "metric", **_GOOD_FALLS[2]}}]),
        )
    )
    content, dashboard = stored["slides"]
    metric, grid = content["blocks"]
    tiles = [metric, *grid["items"], *dashboard["tiles"], dashboard["panels"][0]["block"]]
    assert len(tiles) == 8
    for tile in tiles:
        assert (tile["trend"], tile["delta"], tile["delta_tone"]) in {
            (fall["trend"], fall["delta"], fall["delta_tone"]) for fall in _GOOD_FALLS
        }


@pytest.mark.parametrize("tone", ["success", "error", "warning", "muted"])
@pytest.mark.parametrize("trend", ["up", "down", "flat"])
def test_every_delta_tone_is_stored_as_written_with_every_trend(tone: str, trend: str):
    tile = {"value": "EUR 1.7M", "label": "Loss, Q3", "delta": "-18.4% QoQ", "trend": trend, "delta_tone": tone}
    stored = validate_spec(_deck(_content({"type": "metric", **tile})))
    assert {key: stored["slides"][0]["blocks"][0][key] for key in ("trend", "delta_tone")} == {
        "trend": trend,
        "delta_tone": tone,
    }


def test_a_tile_without_a_delta_tone_is_stored_as_before():
    """Unset, the field is not written: a deck saved before it reads back the
    same, and the trend alone colours the delta. A blank is absent, like every
    optional field's."""
    stored = validate_spec(_deck(_content(dict(_KPI_ROW)), _dashboard()))
    tiles = [*stored["slides"][0]["blocks"][0]["items"], *stored["slides"][1]["tiles"]]
    assert all("delta_tone" not in tile for tile in tiles)
    blank = validate_spec(_deck(_content({"type": "metric", **_GOOD_FALLS[0], "delta_tone": " "})))
    assert "delta_tone" not in blank["slides"][0]["blocks"][0]


@pytest.mark.parametrize("bad", ["accent", "info", "green", "Success", 1, True, ["success"]])
def test_a_delta_tone_off_its_scale_is_refused_at_its_field(bad: object):
    tiles = [{**_GOOD_FALLS[0], "delta_tone": bad}, _GOOD_FALLS[1]]
    error = _refused(_deck(_dashboard(tiles=tiles)))
    assert "slides.0.tiles.0.delta_tone" in error
    grid = _refused(_deck(_content({"type": "metrics_grid", "items": [_GOOD_FALLS[0], {**_GOOD_FALLS[1], "delta_tone": bad}]})))
    assert "blocks.0.items.1.delta_tone" in grid


def test_a_quote_figure_has_no_delta_to_tone():
    """A figure under a quote is a plain tile: value, label, tone and nothing else."""
    error = _refused(
        _deck(_content({"type": "quote", "content": "q", "figures": [{"value": "80K", "label": "Employees", "delta_tone": "success"}]}))
    )
    assert "figures.0.delta_tone" in error


def test_the_reference_names_the_delta_tone_and_its_values():
    reference = spec_reference()
    (metric,) = [line for line in reference.splitlines() if line.startswith("- metric:")]
    assert "trend?: Trend, delta_tone?: DeltaTone (colours the delta and its arrow; unset = by trend)" in metric
    assert '- DeltaTone: "success"|"error"|"warning"|"muted"' in reference.splitlines()


def test_the_guidance_says_a_fall_in_costs_loss_churn_or_burn_is_good_news():
    from app.services import presentation_guidance as guidance

    description = guidance.build_generate_presentation_description()
    assert guidance.DELTA_TONE_RULE in description
    for word in ("costs", "loss", "churn", "burn", 'delta_tone "success"', "the arrow keeps the trend"):
        assert word in guidance.DELTA_TONE_RULE, word
    # The one example of a fall that is good news teaches the field, and validates.
    example = guidance.BLOCK_EXAMPLES["metric"]
    assert (example["trend"], example["delta_tone"]) == ("down", "success")
    validate_spec(_deck(_content(example)))
    # The design skill's KPI wall says the same of its two falls.
    skill = (BACKEND_ROOT / "app" / "agents" / "seed_data" / "skill_content" / "presentation-design.md").read_text(
        encoding="utf-8"
    )
    wall = skill.split("### 3.2", 1)[1].split("### 3.3", 1)[0]
    slide = json.loads(re.search(r"```json\n(.*?)\n```", wall, re.S).group(1).replace("\n", " "))
    falls = [item for item in slide["blocks"][0]["items"] if item.get("trend") == "down"]
    assert falls and all(item["delta_tone"] == "success" for item in falls)
    validate_spec(_deck(slide))


def test_the_frontend_mirrors_the_delta_tone():
    """The type the renderer reads, the attribute it writes inside the delta
    only (a plain tile draws no delta), and the rule that colours it after
    the trend's, at the same specificity, so the tone wins."""
    types = (FRONTEND_ROOT / "src" / "types" / "index.ts").read_text(encoding="utf-8")
    union = re.search(r"export type DeckDeltaTone = ([^;]+);", types)
    assert union and tuple(re.findall(r'"([a-z]+)"', union.group(1))) == ("success", "error", "warning", "muted")
    item = types.split("export interface DeckMetricItem ", 1)[1].split("\n}\n", 1)[0]
    assert "delta_tone?: DeckDeltaTone;" in item
    metrics = _read("blocks", "metrics.tsx")
    assert re.search(
        r'className="deck-metric-delta"\s+data-trend=\{trend\}\s+data-tone=\{metric\.delta_tone\}',
        metrics,
    )
    css = _read("presentation.css")
    down = css.index('.deck-root .deck-metric-delta[data-trend="down"] { color: var(--deck-error-text); }')
    toned = css.index(".deck-root .deck-metric-delta[data-tone] { color: var(--tone-text); }")
    assert down < toned


# ── A dashboard's tile size ─────────────────────────────────────────
#
# Six regional ARR tiles whose comparison sublabel wrapped at 20px pushed the
# panel grid down, and nothing in the spec asked for smaller tiles: a `size`
# on the slide or on a tile is an extra input. `tile_size` sets the row's type.

_REGION_TILES = [
    {
        "value": "EUR 12.3M",
        "label": f"Region {letter} ARR",
        "sublabel": "Q3 25 EUR 4.5M \u00b7 Q2 EUR 6.7M",
        "delta": "+34.5% YoY",
        "trend": "up",
    }
    for letter in "ABCDEF"
]


@pytest.mark.parametrize("size", ["sm", "md"])
def test_a_dashboard_takes_a_tile_size(size: str):
    stored = validate_spec(_deck(_dashboard(tiles=_REGION_TILES, tile_size=size)))
    assert stored["slides"][0]["tile_size"] == size


def test_a_dashboard_without_a_tile_size_is_md_and_the_size_is_the_only_difference():
    """A deck saved before the field reads back as one that says ``md``, the
    row as it always drew; nothing else of the slide moves."""
    plain = validate_spec(_deck(_dashboard(tiles=_REGION_TILES)))["slides"][0]
    small = validate_spec(_deck(_dashboard(tiles=_REGION_TILES, tile_size="sm")))["slides"][0]
    assert plain["tile_size"] == "md"

    def rest(slide: dict) -> dict:
        return {key: value for key, value in slide.items() if key not in {"id", "tile_size"}}

    assert rest(plain) == rest(small)


@pytest.mark.parametrize("bad", ["lg", "xs", "small", "SM", 1, True, ["sm"]])
def test_a_tile_size_off_its_scale_is_refused_at_its_field(bad: object):
    error = _refused(_deck({"layout": "title", "title": "T"}, _dashboard(tiles=_REGION_TILES, tile_size=bad)))
    assert "slides.1" in error and "layout=dashboard" in error and "tile_size" in error


def test_the_tile_size_is_the_slides_and_size_is_still_no_dashboard_field():
    """The row has one size, so a tile carries none; ``size`` is a block
    field (a grid's, a table's) and stays an extra input on the slide."""
    tiles = [{**_REGION_TILES[0], "tile_size": "sm"}, *_REGION_TILES[1:]]
    assert "slides.0.tiles.0.tile_size" in _refused(_deck(_dashboard(tiles=tiles)))
    assert "slides.0.size" in _refused(_deck(_dashboard(tiles=_REGION_TILES, size="sm")))


def test_the_reference_names_the_tile_size_on_the_dashboard_line():
    (line,) = [line for line in spec_reference().splitlines() if line.startswith("- dashboard:")]
    assert 'tile_size?: "sm"|"md" = "md" (type of the tile row: sm = value 36px and 16px label / sublabel / delta' in line


def test_the_tool_schema_offers_the_tile_size_to_a_dashboard():
    import uuid

    from app.services.builtin_tools import build_presentation_tools

    defs, _executor = build_presentation_tools(db=None, user_id=str(uuid.uuid4()), conversation_id=None)  # type: ignore[arg-type]
    tool = next(d for d in defs if d.name == "generate_presentation")
    prop = tool.input_schema["properties"]["slides"]["items"]["properties"]["tile_size"]
    assert prop["enum"] == ["sm", "md"]
    assert prop["description"].startswith("dashboard:")


def test_the_design_skill_says_when_a_dashboard_takes_small_tiles():
    skill = (BACKEND_ROOT / "app" / "agents" / "seed_data" / "skill_content" / "presentation-design.md").read_text(
        encoding="utf-8"
    )
    dashboard = skill.split("### 3.19", 1)[1].split("### 3.20", 1)[0]
    assert '`"tile_size":"sm"`' in dashboard and "seven or eight tiles step down on their own" in dashboard
    validate_spec(_deck(json.loads(re.search(r"```json\n(.*?)\n```", dashboard, re.S).group(1).replace("\n", " "))))


def test_the_renderer_draws_the_tile_size_the_spec_stores():
    """The row carries its size (``md`` included, as a grid does), ``sm``
    joins the crowded row's step selector for selector - so the two can never
    drift apart - and adds the smaller notes, off the fine print. The frontend
    type and the inspector carry the field."""
    dashboard = _read("layouts", "dashboard.tsx")
    assert 'data-size={slide.tile_size ?? "md"}' in dashboard
    css = _read("presentation.css")
    for target, rule in (
        (".deck-metric", "{ padding: 12px var(--deck-space-2); }"),
        (".deck-metric-value", "{ font-size: 36px; }"),
        (".deck-sparkline", "{ width: 84px; height: 30px; }"),
    ):
        selectors = ",\n".join(
            f'.deck-root .deck-dashboard-tiles[{attribute}] {target}'
            for attribute in ('data-count="7"', 'data-count="8"', 'data-size="sm"')
        )
        assert f"{selectors} {rule}" in css, target
    notes = ",\n".join(
        f'.deck-root .deck-dashboard-tiles[data-size="sm"] {target}'
        for target in (".deck-metric-label", ".deck-metric-sublabel", ".deck-metric-delta")
    )
    assert f"{notes} {{ font-size: calc(var(--deck-fs-xs) * 0.8); }}" in css
    assert '.deck-dashboard-tiles[data-size="md"]' not in css
    types = (FRONTEND_ROOT / "src" / "types" / "index.ts").read_text(encoding="utf-8")
    assert 'export type DeckDashboardTileSize = "sm" | "md";' in types
    slide = types.split("export interface DashboardSlide ", 1)[1].split("\n}\n", 1)[0]
    assert "tile_size?: DeckDashboardTileSize;" in slide
    schema = _read("slide-schema.ts")
    assert 'export const DASHBOARD_TILE_SIZES = ["sm", "md"] as const;' in schema
    assert 'en("tile_size", "Tile size", DASHBOARD_TILE_SIZES' in schema
