"""The ``formula`` and ``config_cards`` blocks (#10193): the packaging slide as
native blocks - "Core + Modules + Capacity = One license" above example
configurations - validated, collected for their logo geometry and exported as
native PowerPoint shapes (the result card's gradient included)."""

from __future__ import annotations

import copy
import io

import pytest
from pptx import Presentation
from pptx.enum.dml import MSO_FILL
from pydantic import TypeAdapter, ValidationError

from app.services import pptx_writer
from app.services.presentation_guidance import BLOCK_EXAMPLES
from app.services.presentation_spec import (
    BLOCK_TYPES,
    MAX_CONFIG_CARD_ROWS,
    MAX_CONFIG_CARDS,
    MAX_FORMULA_TERMS,
    ConfigCardsBlock,
    FormulaBlock,
    LeafBlock,
    logo_file_ids,
)

_FILE_A = "11111111-1111-4111-8111-111111111111"
_FILE_B = "22222222-2222-4222-8222-222222222222"
_FILE_C = "33333333-3333-4333-8333-333333333333"


def _formula(**extra) -> dict:
    return {
        "type": "formula",
        "terms": [
            {"title": "Core platform", "icon": "sparkles"},
            {"title": "Modules used", "logos": [_FILE_A, _FILE_B]},
            {"title": "Agentic capacity", "icon": "cpu", "tone": "success"},
            {"title": "One platform license", "variant": "result"},
        ],
        **extra,
    }


def _config(cards: int = 4, rows: int = 3) -> dict:
    return {
        "type": "config_cards",
        "cards": [
            {
                "title": f"Configuration {index + 1}",
                "rows": [
                    {"logo_file_id": _FILE_C, "label": "Module", "chip": "Included", "chip_tone": "success"}
                    if row % 2 == 0
                    else {"label": "Module", "chip": "Available, not used", "state": "dimmed"}
                    for row in range(rows)
                ],
                "footer": "Standard capacity",
            }
            for index in range(cards)
        ],
    }


def test_both_blocks_are_in_the_catalogue_with_a_valid_example():
    for kind, model in (("formula", FormulaBlock), ("config_cards", ConfigCardsBlock)):
        assert kind in BLOCK_TYPES
        model.model_validate(copy.deepcopy(BLOCK_EXAMPLES[kind]))


def test_the_operators_default_to_plus_then_equals():
    assert FormulaBlock.model_validate(_formula()).resolved_operators() == ["+", "+", "="]
    block = FormulaBlock.model_validate(_formula(operators=["+", "x", "="]))
    assert block.resolved_operators() == ["+", "x", "="]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda spec: spec.update(operators=["+"]), "needs 3 operator"),
        (lambda spec: spec["terms"][0].update(variant="result"), "at most one result term"),
        (lambda spec: spec["terms"][0].update(logo_file_id=_FILE_A), "ONE of icon"),
        (lambda spec: spec.update(terms=spec["terms"][:1]), "at least 2"),
        (
            lambda spec: spec.update(terms=[{"title": "T"}] * (MAX_FORMULA_TERMS + 1)),
            f"at most {MAX_FORMULA_TERMS}",
        ),
    ],
)
def test_a_malformed_formula_is_refused_with_its_reason(mutate, message):
    spec = _formula()
    mutate(spec)
    with pytest.raises(ValidationError, match=message):
        FormulaBlock.model_validate(spec)


def test_configuration_cards_bounds_and_rows():
    ConfigCardsBlock.model_validate(_config(cards=MAX_CONFIG_CARDS, rows=MAX_CONFIG_CARD_ROWS))
    with pytest.raises(ValidationError):
        ConfigCardsBlock.model_validate(_config(cards=1))
    with pytest.raises(ValidationError):
        ConfigCardsBlock.model_validate(_config(cards=MAX_CONFIG_CARDS + 1))
    with pytest.raises(ValidationError):
        ConfigCardsBlock.model_validate(_config(rows=MAX_CONFIG_CARD_ROWS + 1))
    spec = _config()
    spec["cards"][0]["rows"][0] = {"chip": "Included"}
    with pytest.raises(ValidationError, match="logo_file_id or a label"):
        ConfigCardsBlock.model_validate(spec)
    row = ConfigCardsBlock.model_validate(_config()).cards[0].rows[1]
    assert row.state == "dimmed"


def test_their_logos_are_collected_for_the_geometry():
    spec = {"slides": [{"layout": "content", "blocks": [_formula(), _config(cards=2, rows=1)]}]}
    assert logo_file_ids(spec) == [_FILE_A, _FILE_B, _FILE_C]


def test_a_light_logo_variant_is_collected_and_needs_its_logo():
    formula = _formula()
    formula["terms"][0] = {"title": "Core", "logo_file_id": _FILE_A, "logo_light_file_id": _FILE_B}
    config = _config(cards=2, rows=1)
    config["cards"][0]["rows"][0]["logo_light_file_id"] = _FILE_A
    spec = {"slides": [{"layout": "content", "blocks": [formula, config]}]}
    FormulaBlock.model_validate(formula)
    ConfigCardsBlock.model_validate(config)
    assert set(logo_file_ids(spec)) == {_FILE_A, _FILE_B, _FILE_C}
    with pytest.raises(ValidationError, match="give logo_file_id too"):
        FormulaBlock.model_validate(
            _formula(terms=[{"title": "A", "logo_light_file_id": _FILE_A}, {"title": "B", "variant": "result"}])
        )


def test_a_row_logo_takes_a_light_variant_and_a_bare_id_still_reads():
    formula = _formula()
    formula["terms"][1]["logos"] = [{"file_id": _FILE_A, "logo_light_file_id": _FILE_C}, _FILE_B]
    term = FormulaBlock.model_validate(formula).terms[1]
    assert [(logo.file_id, logo.logo_light_file_id) for logo in term.logos] == [(_FILE_A, _FILE_C), (_FILE_B, None)]
    spec = {"slides": [{"layout": "content", "blocks": [formula]}]}
    assert logo_file_ids(spec) == [_FILE_A, _FILE_C, _FILE_B]


@pytest.mark.parametrize("color", ["brand_1", "brand_4", "chart_6", "accent", "muted"])
def test_cards_bars_and_chips_take_a_tone_or_a_palette_colour(color):
    formula = _formula()
    formula["terms"][1]["tone"] = color
    FormulaBlock.model_validate(formula)
    config = _config()
    config["cards"][0]["tone"] = color
    config["cards"][0]["rows"][0].update(chip_tone=color, tinted=True)
    row = ConfigCardsBlock.model_validate(config).cards[0].rows[0]
    assert row.chip_tone == color and row.tinted is True
    config["cards"][0]["rows"][0]["chip_tone"] = "purple"
    with pytest.raises(ValidationError):
        ConfigCardsBlock.model_validate(config)


def test_fidelity_options_default_and_validate():
    block = FormulaBlock.model_validate(_formula())
    assert block.align == "center" and all(term.show_title for term in block.terms)
    formula = _formula(align="start")
    formula["terms"][1]["show_title"] = False
    assert FormulaBlock.model_validate(formula).terms[1].show_title is False
    formula["terms"][0]["show_title"] = False
    with pytest.raises(ValidationError, match="show_title false needs a logo_file_id or logos"):
        FormulaBlock.model_validate(formula)
    config = _config(cards=2)
    assert ConfigCardsBlock.model_validate(config).row_style == "outlined"
    config.update(heading="Example configurations", row_style="plain")
    config["cards"][0]["footer_value"] = "Included allowance"
    parsed = ConfigCardsBlock.model_validate(config)
    assert (parsed.heading, parsed.row_style, parsed.cards[0].footer_value) == (
        "Example configurations",
        "plain",
        "Included allowance",
    )
    del config["cards"][0]["footer"]
    with pytest.raises(ValidationError, match="give footer"):
        ConfigCardsBlock.model_validate(config)


def test_the_blocks_are_leaf_blocks_a_column_may_hold():
    adapter = TypeAdapter(LeafBlock)
    assert isinstance(adapter.validate_python(_formula()), FormulaBlock)
    assert isinstance(adapter.validate_python(_config()), ConfigCardsBlock)


def _write(items: list[dict]) -> Presentation:
    slide = {"index": 0, "w": 1920, "h": 1080, "background": {"r": 255, "g": 255, "b": 255, "a": 1}, "items": items}
    data = pptx_writer.build_native_pptx([slide], captures={}, aspect_ratio="16:9")
    return Presentation(io.BytesIO(data))


def test_a_gradient_rect_is_a_native_gradient_fill():
    gradient = {"stops": [{"r": 0, "g": 40, "b": 120, "a": 1}, {"r": 0, "g": 160, "b": 255, "a": 1}], "angle": 120}
    deck = _write(
        [{"kind": "rect", "x": 10, "y": 10, "w": 400, "h": 200, "opacity": 1, "gradient": gradient, "radius": 12}]
    )
    shape = deck.slides[0].shapes[0]
    assert shape.fill.type == MSO_FILL.GRADIENT
    colors = [str(stop.color.rgb) for stop in shape.fill.gradient_stops]
    assert colors == ["002878", "00A0FF"]
    assert shape.fill.gradient_angle == pytest.approx(30.0)


def test_a_dashed_frame_keeps_its_dash():
    line = {"r": 120, "g": 120, "b": 120, "a": 1, "width": 1}
    deck = _write(
        [
            {
                "kind": "rect",
                "x": 10,
                "y": 10,
                "w": 160,
                "h": 32,
                "opacity": 1,
                "line": line,
                "dash": "dash",
                "radius": 16,
            }
        ]
    )
    shape = deck.slides[0].shapes[0]
    assert shape.line.dash_style is not None
