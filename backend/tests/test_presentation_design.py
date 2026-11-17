"""Template design tokens: the reader, the validator and the seed."""

from __future__ import annotations

from app.services.presentation_design import (
    TemplateDesign,
    normalize_template_design,
    template_design,
)
from app.services.template_container_seed import _FILIGRAN_BRAND_DESIGN

FLAT = {
    "shadows": False,
    "glow": False,
    "eyebrows": False,
    "gradient_strokes": "never",
    "radius": {"card": 12, "inner": 8, "chip": 4, "table": 0},
    "spacing": {"margin_x": 96, "margin_top": 72, "footer": 56, "tile_gap": 24},
    "body_align": "top",
}


def test_no_declaration_reads_none() -> None:
    assert template_design(None) is None
    assert template_design({}) is None
    assert template_design({"design": "flat"}) is None
    assert template_design({"design": {}}) is None


def test_flat_declaration_round_trips() -> None:
    design = template_design({"design": FLAT})
    assert isinstance(design, TemplateDesign)
    assert design.as_dict() == FLAT


def test_reader_skips_what_it_cannot_read() -> None:
    design = template_design(
        {
            "design": {
                "shadows": "no",
                "glow": False,
                "gradient_strokes": "sometimes",
                "radius": {"card": 12, "chip": -1, "inner": True, "bogus": 3},
                "spacing": {"margin_x": 9999, "tile_gap": 24},
                "body_align": "bottom",
            }
        }
    )
    assert design is not None
    assert design.as_dict() == {"glow": False, "radius": {"card": 12}, "spacing": {"tile_gap": 24}}


def test_validator_names_every_problem() -> None:
    canonical, problems = normalize_template_design(
        {"shadows": "no", "colour": "blue", "radius": {"card": 100}, "spacing": [], "body_align": "x"}
    )
    assert canonical is None
    assert any("design.shadows" in p for p in problems)
    assert any("design.colour" in p for p in problems)
    assert any("design.radius.card" in p for p in problems)
    assert any("design.spacing must be an object" in p for p in problems)
    assert any("design.body_align" in p for p in problems)


COMPOSED = {
    **FLAT,
    "title_underline": False,
    "title_ink": "brand",
    "cover": "brand",
    "section": "outline",
    "agenda": "plain",
}


def test_compositions_round_trip() -> None:
    design = template_design({"design": COMPOSED})
    assert design is not None
    assert design.as_dict() == COMPOSED
    assert normalize_template_design(COMPOSED) == (COMPOSED, [])


def test_compositions_skip_unknown_values() -> None:
    raw = {"title_underline": "no", "title_ink": "red", "cover": "mosaic", "section": "ghost", "agenda": "cards"}
    assert template_design({"design": raw}) is None
    _, problems = normalize_template_design(raw)
    for key in raw:
        assert any(f"design.{key}" in p for p in problems), key


def test_cover_emblem_round_trips() -> None:
    raw = {"cover": "brand", "cover_emblem": {"scale": 120, "anchor": "right", "opacity": 5}}
    design = template_design({"design": raw})
    assert design is not None
    assert design.as_dict() == raw
    assert normalize_template_design(raw) == (raw, [])


def test_cover_emblem_keeps_what_it_can_read() -> None:
    raw = {"cover_emblem": {"scale": 300, "anchor": "top_left", "opacity": 7, "spin": 1}}
    design = template_design({"design": raw})
    assert design is not None
    assert dict(design.cover_emblem) == {"opacity": 7}
    canonical, problems = normalize_template_design(raw)
    assert canonical == {"cover_emblem": {"opacity": 7}}
    assert any("cover_emblem.scale" in p for p in problems)
    assert any("cover_emblem.anchor" in p for p in problems)
    assert any("cover_emblem.spin" in p for p in problems)


def test_cover_emblem_refuses_a_higher_opacity_and_a_non_object() -> None:
    assert template_design({"design": {"cover_emblem": {"opacity": 40}}}) is None
    _, problems = normalize_template_design({"cover_emblem": {"opacity": True}})
    assert any("cover_emblem.opacity" in p for p in problems)
    _, problems = normalize_template_design({"cover_emblem": 95})
    assert problems == ["design.cover_emblem must be an object"]


def test_a_template_without_cover_emblem_serves_none() -> None:
    design = template_design({"design": {"cover": "brand"}})
    assert design is not None
    assert "cover_emblem" not in design.as_dict()


def test_validator_accepts_the_flat_shape() -> None:
    canonical, problems = normalize_template_design(FLAT)
    assert problems == []
    assert canonical == FLAT


def test_filigran_seed_declares_the_flat_design() -> None:
    design = template_design({"design": _FILIGRAN_BRAND_DESIGN})
    assert design is not None
    assert design.shadows is False and design.glow is False and design.eyebrows is False
    assert design.gradient_strokes == "never"
    assert dict(design.radius) == {"card": 12, "inner": 8, "chip": 4, "table": 0}
    assert dict(design.spacing) == {
        "margin_x": 96,
        "margin_top": 72,
        "footer": 56,
        "title_body": 40,
        "title_subtitle": 12,
        "tile_gap": 24,
        "block_gap": 32,
        "block_gap_max": 48,
        "card_padding": 24,
        "kpi_padding": 20,
        "banner_padding": 32,
    }
    assert design.body_align == "top"
    # Slice 4: the brand compositions.
    # The bar under the body slide titles is back on this template.
    assert design.title_underline is True
    assert (design.title_ink, design.cover, design.section, design.agenda) == ("brand", "brand", "outline", "plain")
    # The larger cover and closing emblem, cropped bottom right.
    assert dict(design.cover_emblem) == {"scale": 95, "anchor": "bottom_right", "opacity": 7}
    # The seed stores exactly the canonical shape.
    assert normalize_template_design(_FILIGRAN_BRAND_DESIGN) == (_FILIGRAN_BRAND_DESIGN, [])
