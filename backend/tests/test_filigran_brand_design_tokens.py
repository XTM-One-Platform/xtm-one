"""The Filigran Brand Deck wears the PPT Design System tokens.

Brand & Communication's final decisions of 28 Oct 2026 (PPT Design System,
section 3) set every colour of the seeded ``filigran-brand`` template: the
backgrounds, the ink, the accents, the block gradients G5 / G6, the feedback
tones, the chart series (never a grey) and the brand hues of the architecture
nodes (XTM One purple on brand_1; the brand extended palette for the
others, no yellow or orange). The other
built-in templates keep their look: the THREAD and XTM One decks are not
touched, and the Filigran video templates keep the palette they had.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.presentation_chrome import normalize_template_chrome, render_chrome_pattern, template_chrome
from app.services.presentations import (
    sanitize_theme_config,
    scheme_palette,
    template_theme_variant,
)
from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS
from app.services.template_containers import validate_theme_config, validate_theme_variants

#: The brand extended palette (HTML companion, section 2.1): five base colours,
#: their tints at 80 / 60 / 40 % and two shades each.
_EXTENDED_PALETTE: frozenset[str] = frozenset(
    {
        "#00020c", "#071578", "#001bda", "#0fbcff", "#00f1bd",
        "#33353d", "#394493", "#3349e1", "#3fc9ff", "#33f4ca",
        "#66676d", "#6a73ae", "#6676e9", "#6fd7ff", "#66f7d7",
        "#999a9e", "#9ca1c9", "#99a4f0", "#9fe4ff", "#99f9e5",
        "#0f1e38", "#05105a", "#0014a3", "#0b8dbf", "#00b58e",
        "#070d19", "#040b3c", "#000e6d", "#085e80", "#00795f",
    }
)

#: Spec 3.1 and 3.9, per scheme, exactly as decided.
_SPEC: dict[str, dict[str, str]] = {
    "dark": {
        "background": "#0a0c1c",
        "surface": "#0d172b",
        "line": "#1f3965",
        "foreground": "#fbfbfb",
        "muted": "#fbfbfb99",
        "accent": "#0fbcff",
        "gradient_from": "#0fbcff",
        "gradient_to": "#00f1bd",
        "success": "#4caf50",
        "warning": "#f9a825",
        "error": "#f44336",
        "chart_1": "#0fbcff",
        "chart_2": "#00f1bd",
        "chart_3": "#3378ff",
        "chart_4": "#9fe4ff",
        "chart_5": "#16ebf9",
        "chart_6": "#7587ff",
    },
    "light": {
        "background": "#e6f7ff",
        "surface": "#ffffff",
        "line": "#001bda33",
        "foreground": "#00020c",
        "muted": "#00020ca6",
        "accent": "#001bda",
        "info": "#071578",
        "gradient_from": "#001bda",
        "gradient_to": "#0fbcff",
        "success": "#4caf50",
        "warning": "#f9a825",
        "error": "#d32f2f",
        "chart_1": "#001bda",
        "chart_2": "#00b58e",
        "chart_3": "#071578",
        "chart_4": "#0fbcff",
        "chart_5": "#3378ff",
        "chart_6": "#7587ff",
    },
}


def _deck(slug: str) -> dict:
    return next(entry for entry in BUILTIN_TEMPLATE_CONTAINERS if entry["slug"] == slug)


def _palette(scheme: str) -> dict[str, str]:
    deck = _deck("filigran-brand")
    return scheme_palette(
        sanitize_theme_config(deck["theme_config"]),
        template_theme_variant(deck["extra_metadata"], scheme),
        scheme,
    )


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_every_spec_token_is_what_a_deck_reads(scheme: str):
    palette = _palette(scheme)
    assert {key: palette.get(key) for key in _SPEC[scheme]} == _SPEC[scheme]


def _luminance(color: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    hex_ = color.lstrip("#")[:6]
    r, g, b = (int(hex_[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _mix(tone: str, paper: str, share: float) -> str:
    """``color-mix(in srgb, tone share, paper)``, the tint a toned box wears."""
    t, p = tone.lstrip("#"), paper.lstrip("#")
    return "#" + "".join(
        f"{round(int(t[i : i + 2], 16) * share + int(p[i : i + 2], 16) * (1 - share)):02x}" for i in (0, 2, 4)
    )


@pytest.mark.parametrize("tone", ["success", "warning", "error"])
def test_a_feedback_tone_as_text_reads_on_every_light_surface(tone: str):
    """Brand decision A: on light, a title or a value drawn in a tone
    takes the palette's text tone, 4.5:1 at least on the page, the card, the
    #DAF1FF ice tint, the callout (4%) and table cell (9%) tints and its own
    16% tile (org chart seat initials); the fill tone
    stays the spec's."""
    palette = _palette("light")
    text = palette[f"{tone}_text"]
    fill = palette[tone]
    surfaces = [
        palette["background"],
        palette["surface"],
        "#daf1ff",
        _mix(fill, palette["surface"], 0.04),
        _mix(fill, palette["surface"], 0.09),
        # .deck-org-media-tile: color-mix(in srgb, var(--tone) 16%, var(--deck-surface)).
        _mix(fill, palette["surface"], 0.16),
    ]
    assert min(_contrast(text, surface) for surface in surfaces) >= 4.5, (tone, text)
    assert fill == _SPEC["light"][tone]


@pytest.mark.parametrize("tone", ["success", "warning", "error"])
def test_the_dark_scheme_keeps_its_tones_as_text(tone: str):
    """No text tone on dark: the spec's tones read at 4.5:1 or more on #0A0C1C
    and #0D172B, so the text falls back to the tone itself."""
    palette = _palette("dark")
    assert f"{tone}_text" not in palette
    for surface in (palette["background"], palette["surface"]):
        assert _contrast(palette[tone], surface) >= 4.5, (tone, surface)


_PRESENTATION_CSS = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation" / "presentation.css"
)

#: The SVG texts of the architecture and org chart blocks that read a tone:
#: being text, they paint the text tone, never the fill tone.
_SVG_TONED_TEXT_RULES = (
    ".deck-root .deck-architecture-svg tspan[data-tone]",
    ".deck-root .deck-org-svg .deck-org-number",
)


def _css_rule(selector: str) -> str:
    css = re.sub(r"/\*.*?\*/", "", _PRESENTATION_CSS.read_text(encoding="utf-8"), flags=re.S)
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"{selector} is gone from presentation.css"
    return match.group(1)


@pytest.mark.parametrize("selector", _SVG_TONED_TEXT_RULES)
def test_a_toned_svg_text_paints_the_text_tone(selector: str):
    body = _css_rule(selector)
    assert "fill: var(--tone-text)" in body, (selector, body)
    assert not re.search(r"fill:\s*var\(--tone\)", body), (selector, body)


@pytest.mark.parametrize("selector", [".deck-root .deck-arch-zone-title", ".deck-root .deck-arch-edge-text"])
def test_architecture_titles_and_edge_labels_are_drawn_in_the_ink(selector: str):
    """`.deck-architecture-svg text { fill: var(--deck-fg) }`
    outranks these rules, so a tone fill on them never rendered; none is declared."""
    assert "fill" not in _css_rule(selector), selector


def test_tone_markup_inside_a_core_node_is_drawn_in_the_ink():
    """A toned tspan on a core node's tint fell to 2.2:1.
    The emphasis is carried by an underline instead."""
    body = _css_rule('.deck-root .deck-arch-node[data-emphasis="core"] tspan[data-tone]')
    assert "fill: var(--deck-fg)" in body
    assert "text-decoration: underline" in body
    assert "font-weight" not in body
    # Chromium computes text-underline-offset to auto on an SVG tspan.
    assert "text-underline-offset" not in body


def _hex(color: str) -> str:
    """A palette colour as ``#rrggbb``: hex as is, ``hsl(h s% l%)`` converted."""
    if color.startswith("#"):
        return color[:7].lower()
    import colorsys

    h, s, lightness = (float(part.rstrip("%")) for part in color[4:-1].split())
    r, g, b = colorsys.hls_to_rgb(h / 360, lightness / 100, s / 100)
    return "#" + "".join(f"{round(c * 255):02x}" for c in (r, g, b))


_TONES = ("accent", "info", "success", "warning", "error")
_TILE_KEYS = ("background", "surface", "foreground", *_TONES, "success_text", "warning_text", "error_text")


def _scheme_palettes() -> list[tuple[str, str, dict[str, str]]]:
    """Every scheme of the Filigran and XTM One brand decks, its colours as hex."""
    out = []
    for slug in ("filigran-brand", "xtm-one-brand"):
        deck = _deck(slug)
        for scheme in deck["extra_metadata"]["supported_schemes"]:
            palette = scheme_palette(
                sanitize_theme_config(deck["theme_config"]),
                template_theme_variant(deck["extra_metadata"], scheme),
                scheme,
            )
            out.append((slug, scheme, {key: _hex(palette[key]) for key in _TILE_KEYS if key in palette}))
    return out


@pytest.mark.parametrize(("slug", "scheme", "palette"), _scheme_palettes(), ids=lambda v: v if isinstance(v, str) else "")
def test_org_chart_initials_read_on_their_tile_in_every_scheme(slug: str, scheme: str, palette: dict[str, str]):
    """The initials (20 px / 600, body text) paint the text
    tone mixed 75% with the ink, on the 16% tile of the tone: 4.5:1 at least
    on dark and light (before: #F44336 4.20:1, XTM One error 4.00:1 and
    accent #A64DFF 3.58:1 on dark)."""
    body = _css_rule(".deck-root .deck-org-svg .deck-org-initials text")
    assert "fill: color-mix(in srgb, var(--tone-text) 75%, var(--deck-fg))" in body
    for tone in _TONES:
        fill = palette[tone]
        text = palette.get(f"{tone}_text", fill)
        tile = _mix(fill, palette["surface"], 0.16)
        initials = _mix(text, palette["foreground"], 0.75)
        assert _contrast(initials, tile) >= 4.5, (slug, scheme, tone, initials, tile)


#: The ink a filled badge's text takes, per scheme (``--tone-ink``): the page on
#: dark; on light the ink for the pale fills (success, warning), the page else.
_BADGE_INK = {
    "dark": dict.fromkeys(_TONES, "background"),
    "light": {
        "accent": "background",
        "info": "background",
        "error": "background",
        "success": "foreground",
        "warning": "foreground",
    },
}


@pytest.mark.parametrize(("slug", "scheme", "palette"), _scheme_palettes(), ids=lambda v: v if isinstance(v, str) else "")
def test_org_chart_badge_text_reads_on_its_filled_tone(slug: str, scheme: str, palette: dict[str, str]):
    """The vacant seat's "Open" pill on light painted
    #E6F7FF on #F9A825 (1.79:1); the badge text now takes ``--tone-ink``."""
    assert "fill: var(--tone-ink, var(--deck-bg))" in _css_rule(".deck-root .deck-org-svg .deck-org-badge-text")
    for tone in _TONES:
        ink = palette[_BADGE_INK[scheme][tone]]
        assert _contrast(ink, palette[tone]) >= 4.5, (slug, scheme, tone, ink, palette[tone])


#: The thinnest badge pairs the brand decks paint today:
#: XTM One Brand dark accent 4.61:1, Filigran Brand light error 4.53:1.
#: Pinned to the hundredth so a palette tweak that thins them fails here, with
#: the ratio named, before it reaches 4.5:1 on a slide.
_THIN_BADGES = {
    ("xtm-one-brand", "dark", "accent"): 4.61,
    ("filigran-brand", "light", "error"): 4.53,
}


@pytest.mark.parametrize(("slug", "scheme", "tone"), sorted(_THIN_BADGES))
def test_the_thin_badge_ratios_keep_their_margin(slug: str, scheme: str, tone: str):
    palette = next(p for s, sc, p in _scheme_palettes() if (s, sc) == (slug, scheme))
    ratio = _contrast(palette[_BADGE_INK[scheme][tone]], palette[tone])
    assert ratio >= _THIN_BADGES[(slug, scheme, tone)] - 0.005, (slug, scheme, tone, round(ratio, 3))


def test_the_badge_ink_tokens_follow_the_scheme():
    css = _PRESENTATION_CSS.read_text(encoding="utf-8")
    dark = css[css.index(".deck-root [data-scheme=\"dark\"] {") :]
    dark = dark[: dark.index("}")]
    light = css[css.index(".deck-root [data-scheme=\"light\"] {") :]
    light = light[: light.index("}")]
    for tone in ("success", "warning"):
        assert f"--deck-{tone}-ink: var(--deck-bg);" in dark
        assert f"--deck-{tone}-ink: var(--deck-fg);" in light


def test_the_seed_is_accepted_whole_by_the_template_validators():
    """Nothing the seed declares is dropped on the way to a deck: the 8-digit
    hex of the muted ink and the light stroke included."""
    deck = _deck("filigran-brand")
    accepted, problems = validate_theme_config(deck["theme_config"], kind="presentation")
    assert problems == [] and accepted == deck["theme_config"]
    variants, problems = validate_theme_variants(deck["extra_metadata"], kind="presentation")
    assert problems == []
    assert variants["theme_variants"]["light"] == deck["extra_metadata"]["theme_variants"]["light"]


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_no_grey_in_a_chart_series(scheme: str):
    """Spec 3.0 rule 4 and 3.9: no grey in any series, in any theme."""
    palette = _palette(scheme)
    for index in range(1, 7):
        value = palette[f"chart_{index}"].lstrip("#")
        red, green, blue = (int(value[i : i + 2], 16) for i in (0, 2, 4))
        assert max(red, green, blue) - min(red, green, blue) > 48, (scheme, index, palette[f"chart_{index}"])


#: The XTM One product purple on brand_1 (decision of 3 Nov 2026, a
#: deviation from the "no purple" rule), the XTM One template's own hue.
_XTM_ONE_PURPLE = {"dark": "#a64dff", "light": "#5e1ad5"}

#: The surfaces an architecture zone border, tint and chip sit on, per scheme.
_ARCHITECTURE_SURFACES = {
    "dark": ("#0a0c1c", "#0d172b"),
    "light": ("#e6f7ff", "#ffffff", "#daf1ff"),
}


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_the_brand_hues_come_from_the_extended_palette(scheme: str):
    """The architecture hues: XTM One purple on brand_1, the brand extended
    palette for OpenCTI, OpenAEV and OpenCRQ, four distinct ones."""
    palette = _palette(scheme)
    hues = [palette[f"brand_{index}"] for index in range(1, 5)]
    assert hues[0] == _XTM_ONE_PURPLE[scheme]
    assert set(hues[1:]) <= _EXTENDED_PALETTE, hues
    assert len(set(hues)) == 4


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_the_xtm_one_purple_holds_three_to_one_on_every_architecture_surface(scheme: str):
    """The zone border and chip drawn in brand_1 stay at 3:1 or more on the
    page, the card and the light tint, in both schemes."""
    hue = _palette(scheme)["brand_1"]
    for surface in _ARCHITECTURE_SURFACES[scheme]:
        assert _contrast(hue, surface) >= 3.0, (scheme, hue, surface)


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_no_purple_in_a_chart_series(scheme: str):
    """The purple is the XTM One product hue only: never a chart series."""
    palette = _palette(scheme)
    series = {palette[f"chart_{index}"].lower() for index in range(1, 7)}
    assert not series & set(_XTM_ONE_PURPLE.values())


def test_the_light_second_accent_is_never_aqua_as_text():
    """Spec 3.1: on light the second accent is Navy; Aqua only on fills."""
    light = _palette("light")
    assert light["info"] == "#071578"
    assert "#00f1bd" not in {light[key] for key in ("foreground", "muted", "accent", "info")}


def test_the_chrome_reads_title_event_classification_with_the_emblem_and_no_watermark():
    deck = _deck("filigran-brand")
    canonical, problems = normalize_template_chrome(deck["extra_metadata"]["chrome"])
    assert problems == []
    assert canonical["footer"] == "{{title}} \u00b7 {{event}} \u00b7 {{classification}}"
    read = template_chrome(deck["extra_metadata"])
    assert read is not None and read.emblem is True and read.watermark is False
    pattern = canonical["footer"]
    # Each part optional, the separator dropped with an empty part.
    assert render_chrome_pattern(pattern, {"title": "Q3 Review", "event": "THREAD 2026", "classification": "Internal"}) == (
        "Q3 Review \u00b7 THREAD 2026 \u00b7 Internal"
    )
    assert render_chrome_pattern(pattern, {"title": "Q3 Review", "event": "", "classification": "Internal"}) == (
        "Q3 Review \u00b7 Internal"
    )
    assert render_chrome_pattern(pattern, {"title": "Q3 Review", "event": "", "classification": ""}) == "Q3 Review"


def test_the_chrome_signs_every_body_slide_with_the_page_and_the_copyright():
    """Slice 2: footer right with {{page}} and {{year}}, emblem 48 px
    aligned with the title, and the canonical shape keeps every key."""
    deck = _deck("filigran-brand")
    canonical, problems = normalize_template_chrome(deck["extra_metadata"]["chrome"])
    assert problems == []
    assert canonical["footer_right"] == "{{page}} \u00b7 \u00a9 {{year}} Filigran. All rights reserved."
    assert (canonical["emblem_size"], canonical["emblem_align"]) == (48, "title")
    # One-colour mark, Royal Blue (the light accent) on light.
    assert canonical["emblem_ink"] == "accent"
    read = template_chrome(deck["extra_metadata"])
    assert read is not None and read.as_dict() == canonical
    assert render_chrome_pattern(canonical["footer_right"], {"page": "07", "year": "2026"}) == (
        "07 \u00b7 \u00a9 2026 Filigran. All rights reserved."
    )
    # Slide numbers off: the renderer drops the {{page}} slot with its own
    # separator before filling (chromeFooterRight, pinned by
    # chrome-footer-right.test.ts), so the copyright keeps its sign.


def test_the_template_keeps_its_slide_cap_fonts_and_schemes():
    deck = _deck("filigran-brand")
    assert deck["extra_metadata"]["max_slides"] == 150
    assert deck["extra_metadata"]["supported_schemes"] == ["dark", "light"]
    assert deck["theme_config"]["heading_font"].startswith("'Geologica'")
    assert deck["theme_config"]["body_font"].startswith("'IBM Plex Sans'")
    assert deck["theme_config"]["mono_font"].startswith("'IBM Plex Mono'")


def test_the_other_built_in_templates_keep_their_look():
    """Backward compatible: the XTM One deck keeps its chrome, the Filigran
    videos keep the palette they wore before the redesign."""
    xtm_one = _deck("xtm-one-brand")
    assert xtm_one["extra_metadata"]["chrome"]["footer"] == "{{event}} - {{title}} - {{classification}}"
    assert "emblem" not in xtm_one["extra_metadata"]["chrome"]
    for slug in ("xtm-one-brand", "thread-brand"):
        chrome = next(e for e in BUILTIN_TEMPLATE_CONTAINERS if e["slug"] == slug)["extra_metadata"].get("chrome") or {}
        assert not {"footer_right", "emblem_size", "emblem_align", "emblem_ink"} & set(chrome), slug
    videos = [entry for entry in BUILTIN_TEMPLATE_CONTAINERS if entry["kind"] == "video"]
    filigran_videos = [v for v in videos if (v["extra_metadata"].get("brand") or {}).get("deck") == "filigran-brand"]
    assert filigran_videos, "the Filigran video templates are seeded"
    for video in filigran_videos:
        assert video["theme_config"]["background"] == "#070d18", video["slug"]
        assert video["theme_config"]["accent"] == "#42caff", video["slug"]
