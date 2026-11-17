"""The template form edits the chart series and brand hues.

The form's colour groups are cut from `palette-keys.ts`, whose
`THEME_CONFIG_KEYS` must be exactly the renderer's whitelist; and what the form
saves for those keys is held to the same validation as the rest of the palette,
in the base `theme_config` and in every `theme_variants` palette.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.presentations import (
    BRAND_COLOR_THEME_CONFIG_KEYS,
    CHART_THEME_CONFIG_KEYS,
    SCHEME_BOUND_THEME_KEYS,
    THEME_CONFIG_KEYS,
)
from app.services.template_containers import (
    unsafe_theme_value_problems,
    validate_theme_config,
    validate_theme_variants,
)

PRESENTATION_DIR = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation"
FORM_KEYS = (*CHART_THEME_CONFIG_KEYS, *BRAND_COLOR_THEME_CONFIG_KEYS)


def _read(name: str) -> str:
    return (PRESENTATION_DIR / name).read_text(encoding="utf-8")


def test_palette_keys_mirror_the_renderer_whitelist():
    match = re.search(r"export const THEME_CONFIG_KEYS[^=]*=\s*\[(.*?)\];", _read("palette-keys.ts"), re.DOTALL)
    assert match, "palette-keys.ts must export THEME_CONFIG_KEYS as an array literal"
    assert tuple(re.findall(r'"([a-z_0-9]+)"', match.group(1))) == THEME_CONFIG_KEYS


def test_the_form_and_the_swatches_read_the_one_list():
    editor = _read("TemplateDesignEditor.tsx")
    assert 'from "./palette-keys"' in editor
    assert "keys={CHART_SERIES_KEYS}" in editor and "keys={BRAND_HUE_KEYS}" in editor
    assert not re.search(r'"(chart|brand)_\d"', editor), "the form spells out a key list of its own"
    assert "PALETTE_SWATCH_KEYS.filter(" in _read("TemplateGeneratorDialog.tsx")


def test_the_chart_series_are_scheme_bound_and_the_brand_hues_carry_over():
    # Why the variant grid offers both: the series are tuned per theme, the
    # brand hues are inherited from the base and may be re-tuned.
    assert set(CHART_THEME_CONFIG_KEYS) <= set(SCHEME_BOUND_THEME_KEYS)
    assert not set(BRAND_COLOR_THEME_CONFIG_KEYS) & set(SCHEME_BOUND_THEME_KEYS)


@pytest.mark.parametrize("key", FORM_KEYS)
def test_a_form_value_is_accepted_in_the_base_and_in_a_variant(key: str):
    accepted, problems = validate_theme_config({key: "#1a2b3c"}, kind="presentation")
    assert (accepted, problems) == ({key: "#1a2b3c"}, [])
    metadata, variant_problems = validate_theme_variants(
        {"theme_variants": {"light": {key: "#abcdef"}}}, kind="presentation"
    )
    assert variant_problems == []
    assert metadata == {"theme_variants": {"light": {key: "#abcdef"}}}
    assert unsafe_theme_value_problems({key: "#1a2b3c"}, {"theme_variants": {"dark": {key: "#000000"}}}) == []


@pytest.mark.parametrize("key", FORM_KEYS)
def test_an_unsafe_form_value_is_refused_like_any_palette_value(key: str):
    unsafe = "url(https://example.com/x.png)"
    _accepted, problems = validate_theme_config({key: unsafe}, kind="presentation")
    assert problems and key in problems[0]
    refused = unsafe_theme_value_problems(
        {key: unsafe}, {"theme_variants": {"light": {key: unsafe}}}, kind="presentation"
    )
    assert len(refused) == 2
    assert any("theme_variants.light" in line for line in refused)
