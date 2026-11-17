"""Palette colours on architecture nodes and zones.

A ``tone`` is a meaning; a product box in its product's colour is not one. An
architecture node or zone names a colour of the deck's template palette
(``color``: ``brand_1``..``brand_4`` or ``chart_1``..``chart_6``) and wears it
in place of the tone. The backend half of the contract is pinned here - the
validator, the theme whitelist, the seeded palettes, the tool example - with
the renderer half read off the frontend sources. What the browser computes
from it, and what the PPTX carries, is
``frontend/tests_e2e/presentation-architecture-colors.spec.ts``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.presentation_spec import (
    PALETTE_COLORS,
    PresentationSpecError,
    spec_reference,
    validate_spec,
)
from app.services.presentations import (
    BRAND_COLOR_THEME_CONFIG_KEYS,
    CHART_THEME_CONFIG_KEYS,
    SCHEME_BOUND_THEME_KEYS,
    THEME_CONFIG_KEYS,
    sanitize_theme_config,
    scheme_palette,
    template_theme_variant,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = BACKEND_ROOT.parent / "frontend" / "src"
PRESENTATION_DIR = FRONTEND_SRC / "components" / "presentation"


def _read(*parts: str) -> str:
    return PRESENTATION_DIR.joinpath(*parts).read_text(encoding="utf-8")


def _deck(*, node_color: str | None = None, zone_color: str | None = None) -> dict:
    """One product card and a core inside a platform zone."""
    product: dict = {"id": "cti", "label": "OpenCTI", "icon": "brain", "chips": ["Graph", "Feeds"], "row": 1, "col": 1}
    if node_color is not None:
        product["color"] = node_color
    zone: dict = {"label": "XTM Platform", "row": 1, "col": 1, "col_span": 2}
    if zone_color is not None:
        zone["color"] = zone_color
    block = {
        "type": "architecture",
        "nodes": [
            product,
            {"id": "core", "label": "XTM One", "shape": "circle", "emphasis": "core", "row": 1, "col": 2},
        ],
        "edges": [{"source": "core", "target": "cti", "arrows": "none"}],
        "zones": [zone],
    }
    return {"title": "Deck", "slides": [{"layout": "content", "blocks": [block]}]}


def _block(spec: dict) -> dict:
    return spec["slides"][0]["blocks"][0]


# ── The spec ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("color", PALETTE_COLORS)
def test_a_node_and_a_zone_wear_every_palette_colour(color: str):
    block = _block(validate_spec(_deck(node_color=color, zone_color=color)))
    assert block["nodes"][0]["color"] == color
    assert block["zones"][0]["color"] == color


def test_a_deck_without_a_colour_is_stored_as_before():
    """Existing decks are unchanged: an absent ``color`` is never materialised,
    so re-saving a stored deck writes no new key and the renderer publishes no
    ``data-color`` - the pixels it always had."""
    block = _block(validate_spec(_deck()))
    assert all("color" not in node for node in block["nodes"])
    assert "color" not in block["zones"][0]


@pytest.mark.parametrize("where", ["node", "zone"])
@pytest.mark.parametrize(
    "value",
    [
        "brand_5",  # past the four brand hues
        "brand_0",
        "chart_7",  # past the six series
        "accent",  # a tone is a meaning, not a palette colour
        "xtm_one",  # a product name is not a theme key
        "#7B2FF7",  # a raw colour: the palette is the template's to decide
        "purple",
    ],
)
def test_an_unknown_colour_key_is_refused_naming_the_allowed_ones(where: str, value: str):
    deck = _deck(node_color=value) if where == "node" else _deck(zone_color=value)
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(deck)
    path = "nodes.0.color" if where == "node" else "zones.0.color"
    # The agent's reader: the field address and every value it may use instead.
    message = str(exc.value)
    assert path in message
    for color in PALETTE_COLORS:
        assert f"'{color}'" in message
    # The person behind the REST PATCH gets the same refusal, addressed to the slide.
    assert path in exc.value.human_message()


def test_the_palette_colours_are_the_theme_keys_the_renderer_resolves():
    """Every colour the validator accepts is a key a template may set and the
    renderer maps to a CSS variable - never a value that validates and paints
    nothing (the import-time contract in ``presentations.py`` says the same)."""
    assert BRAND_COLOR_THEME_CONFIG_KEYS == ("brand_1", "brand_2", "brand_3", "brand_4")
    assert (*BRAND_COLOR_THEME_CONFIG_KEYS, *CHART_THEME_CONFIG_KEYS) == PALETTE_COLORS
    assert set(PALETTE_COLORS) <= set(THEME_CONFIG_KEYS)
    reference = spec_reference()
    assert '- PaletteColor: "' + '"|"'.join(PALETTE_COLORS) + '"' in reference
    # the node, the zone, the org chart seat and the positioning map quadrant
    assert reference.count("color?: PaletteColor") == 4


def test_brand_hues_carry_to_either_scheme_and_a_variant_retunes_them():
    """A product colour is brand identity, like the accent: a template that
    declares it once keeps it on a deck of the other scheme, and a
    ``theme_variants`` palette may re-tune it for its own paper."""
    assert not set(BRAND_COLOR_THEME_CONFIG_KEYS) & set(SCHEME_BOUND_THEME_KEYS)
    dark = {"background": "#070d18", "accent": "#42caff", "chart_1": "#42caff", "brand_1": "#a64dff"}
    light = scheme_palette(dark, {}, "light")
    assert light["brand_1"] == "#a64dff"
    assert "chart_1" not in light  # the series stay with the surface they were tuned for
    assert scheme_palette(dark, {"brand_1": "#5e1ad5"}, "light")["brand_1"] == "#5e1ad5"


def test_template_authoring_honours_the_brand_hues_and_refuses_an_unknown_key():
    from app.services.template_containers import validate_theme_config

    accepted, problems = validate_theme_config(
        {"brand_1": "#a64dff", "brand_4": "hsl(233 100% 73%)", "brand_5": "#ffffff", "xtm_one": "#7B2FF7"}
    )
    assert accepted == {"brand_1": "#a64dff", "brand_4": "hsl(233 100% 73%)"}
    assert len(problems) == 2
    assert any("brand_5" in problem for problem in problems)
    assert any("xtm_one" in problem for problem in problems)
    # A report has no architecture block: its whitelist does not take the hues.
    report_accepted, report_problems = validate_theme_config({"brand_1": "#a64dff"}, kind="report")
    assert report_accepted == {}
    assert report_problems


# ── The seeded brand decks ───────────────────────────────────────────────────

#: XTM One purple, OpenCTI blue, OpenAEV orange, OpenCRQ blue - per scheme.
#: The Filigran deck puts the XTM One purple on brand_1 (a deviation
#: from the "no purple" rule) and draws the other three hues from the
#: brand extended palette (no yellow or orange).
_PRODUCT_COLOURS: dict[str, dict[str, tuple[str, ...]]] = {
    "filigran-brand": {
        "dark": ("#a64dff", "#0fbcff", "#6676e9", "#9fe4ff"),
        "light": ("#5e1ad5", "#001bda", "#0b8dbf", "#071578"),
    },
    # The THREAD Color Set's nearest: Purple, Cyan / Blue, Orange, Blue / Cobalt Blue.
    "thread-brand": {
        "dark": ("#e184ff", "#00e0ff", "#ff8558", "#3492ff"),
        "light": ("#e184ff", "#3492ff", "#ff8558", "#001bda"),
    },
    "xtm-one-brand": {
        "dark": ("hsl(270 100% 65%)", "#0fbcff", "#f2933a", "#7587ff"),
    },
}


@pytest.mark.parametrize(
    ("slug", "scheme"),
    [(slug, scheme) for slug, schemes in _PRODUCT_COLOURS.items() for scheme in schemes],
)
def test_the_brand_decks_give_every_product_box_its_colour(slug: str, scheme: str):
    """Resolved exactly as a deck read resolves them (``resolve_template_palette``):
    the base palette, the scheme's variant laid over it."""
    from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS

    container = next(item for item in BUILTIN_TEMPLATE_CONTAINERS if item["slug"] == slug)
    assert scheme in container["extra_metadata"]["supported_schemes"]
    palette = scheme_palette(
        sanitize_theme_config(container["theme_config"]),
        template_theme_variant(container["extra_metadata"], scheme),
        scheme,
    )
    hues = tuple(palette[key] for key in BRAND_COLOR_THEME_CONFIG_KEYS)
    assert hues == _PRODUCT_COLOURS[slug][scheme]
    # Four products, four colours: the two blues never collapse into one.
    assert len({hue.lower() for hue in hues}) == len(hues)


def test_the_tool_example_colours_the_product_boxes_instead_of_borrowing_tones():
    """The ``generate_presentation`` example is what an agent copies: it shows
    each product in its brand hue (the seeded meaning of brand_1..4) rather
    than an ``info`` / ``success`` / ``warning`` tone used as decoration."""
    from app.services.presentation_guidance import BLOCK_EXAMPLES

    nodes = {node["id"]: node for node in BLOCK_EXAMPLES["architecture"]["nodes"]}
    assert {node_id: nodes[node_id].get("color") for node_id in ("core", "cti", "aev", "crq")} == {
        "core": "brand_1",
        "cti": "brand_2",
        "aev": "brand_3",
        "crq": "brand_4",
    }
    assert not any("tone" in nodes[node_id] for node_id in ("core", "cti", "aev", "crq"))


# ── The renderer (frontend tripwires) ────────────────────────────────────────


def _rule(css: str, selector: str) -> str:
    """The declarations of the rule whose selector list ends with *selector*."""
    start = css.index(f"{selector} {{")
    return css[start : css.index("}", start)]


def test_the_stylesheet_resolves_every_palette_colour_to_its_theme_variable():
    """``data-color`` points ``--tone`` at the CSS variable ``theme.ts`` maps
    the same key to - the variable a template's inline palette sets - so the
    colour a node names is the colour its template declared, per slide."""
    css = _read("presentation.css")
    variables = dict(re.findall(r'^\s*(\w+): "(--deck-[\w-]+)",$', _read("theme.ts"), re.MULTILINE))
    for color in PALETTE_COLORS:
        assert f'.deck-root [data-color="{color}"] {{ --tone: var({variables[color]}); }}' in css, color
    # The colour rules come after the tone rules: equal specificity, the colour wins.
    assert css.index('[data-color="brand_1"]') > css.index('[data-tone="muted"] { --tone')


def test_each_scheme_declares_the_brand_hues_and_how_strongly_a_colour_tints():
    """The tint is 18% on a dark slide root and 10% on a light one, and the
    brand hues fall back to the series of the same rank IN THAT SCHEME: both
    are re-declared on every slide root, never inherited from the deck root."""
    css = _read("presentation.css")
    for scheme, tint in (("dark", "18%"), ("light", "10%")):
        block = _rule(css, f'.deck-root [data-scheme="{scheme}"]')
        assert f"--deck-color-tint: {tint};" in block, scheme
        for rank in range(1, 5):
            assert f"--deck-brand-{rank}: var(--deck-chart-{rank});" in block, (scheme, rank)


def test_a_coloured_node_and_zone_wear_the_colour_on_the_border_over_a_tint():
    css = _read("presentation.css")
    node = _rule(css, ".deck-root .deck-arch-node[data-color]")
    assert "--arch-stroke: var(--tone);" in node
    assert "--arch-fill: color-mix(in srgb, var(--tone) var(--deck-color-tint), var(--deck-surface-solid));" in node
    shape = _rule(css, ".deck-root .deck-arch-node-shape")
    assert "fill: var(--arch-fill, var(--deck-surface-solid));" in shape
    # A coloured core keeps the colour's tint; an uncoloured one its 18% of the tone.
    core = _rule(css, '.deck-root .deck-arch-node[data-emphasis="core"] .deck-arch-node-shape')
    assert "fill: var(--arch-fill, color-mix(in srgb, var(--tone) 18%, var(--deck-surface-solid)));" in core
    zone = _rule(css, ".deck-root .deck-arch-zone[data-color] .deck-arch-zone-panel")
    assert "fill: color-mix(in srgb, var(--tone) var(--deck-color-tint), transparent);" in zone
    assert "stroke: var(--tone);" in zone
    # The neutral defaults leave a coloured piece alone.
    assert ".deck-root .deck-arch-node:not([data-tone]):not([data-color]) {" in css
    assert ".deck-root .deck-arch-zone:not([data-tone]):not([data-color]) {" in css
    # Chips, the icon tile and the glow read the node's --tone: the colour reaches them.
    for selector in (".deck-root .deck-arch-chip rect", ".deck-root .deck-arch-node-tile", ".deck-root .deck-arch-node-glow"):
        assert "var(--tone)" in _rule(css, selector), selector


def test_the_renderer_publishes_the_colour_instead_of_the_tone():
    view = _read("blocks", "Architecture.tsx")
    assert "data-color={node.color}" in view
    assert "data-tone={node.color ? undefined :" in view
    assert "data-color={color}" in view and "data-tone={color ? undefined : tone}" in view
    assert "color: node.color" in _read("blocks", "architecture-layout.ts")
    types = (FRONTEND_SRC / "types" / "index.ts").read_text(encoding="utf-8")
    union = types.split("export type DeckPaletteColor =", 1)[1].split(";", 1)[0]
    assert tuple(re.findall(r'"(\w+)"', union)) == PALETTE_COLORS
