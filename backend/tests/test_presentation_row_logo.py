"""A picture in place of a list or feature_list row's marker (``logo_file_id``).

An SVG flag or a small logo replaces the row's bullet, glyph or icon tile,
with an optional hairline (``logo_border``) so a white flag reads on a white
card. These pins hold the spec contract: the field validates on both item
models, one medium per row, the border only frames a picture, a feature row
may drop its icon only for a picture, and rows without the field validate as
before. The render side is pinned in ``list-row-logo.test.tsx``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.presentation_spec import FeatureItem, FeatureListBlock, ListBlock, ListItem

FLAG = "83bb4dbc-0000-4000-8000-000000000001"
PRESENTATION_CSS = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation" / "presentation.css"
)


def test_a_list_row_takes_a_picture_and_a_border():
    item = ListItem.model_validate({"text": "**Event**\nCity", "logo_file_id": FLAG, "logo_border": True})
    assert item.logo_file_id == FLAG
    assert item.logo_border is True


def test_a_list_row_without_a_picture_is_unchanged():
    item = ListItem.model_validate({"text": "Point", "icon": "check"})
    assert item.logo_file_id is None
    assert item.logo_border is False
    # Nothing new is written back for a row that does not use the field.
    assert "logo_file_id" not in item.model_dump(exclude_defaults=True)


def test_a_row_has_one_marker():
    with pytest.raises(ValidationError, match="icon OR a logo_file_id"):
        ListItem.model_validate({"text": "Point", "icon": "check", "logo_file_id": FLAG})
    with pytest.raises(ValidationError, match="icon OR a logo_file_id"):
        FeatureItem.model_validate({"title": "Feature", "icon": "check", "logo_file_id": FLAG})


def test_the_border_needs_a_picture():
    with pytest.raises(ValidationError, match="logo_border frames"):
        ListItem.model_validate({"text": "Point", "logo_border": True})
    with pytest.raises(ValidationError, match="logo_border frames"):
        FeatureItem.model_validate({"title": "Feature", "icon": "check", "logo_border": True})


def test_a_feature_row_drops_its_icon_only_for_a_picture():
    assert FeatureItem.model_validate({"title": "Feature", "logo_file_id": FLAG}).icon is None
    with pytest.raises(ValidationError, match="an icon or a logo_file_id"):
        FeatureItem.model_validate({"title": "Feature"})


def test_mixed_rows_validate_in_one_block():
    ListBlock.model_validate(
        {
            "type": "list",
            "items": [
                {"text": "**Paris**\nFrance", "logo_file_id": FLAG},
                {"text": "**Tokyo**\nJapan", "logo_file_id": FLAG, "logo_border": True},
                {"text": "No flag"},
            ],
        }
    )
    FeatureListBlock.model_validate(
        {
            "type": "feature_list",
            "items": [
                {"title": "With a logo", "logo_file_id": FLAG},
                {"title": "With an icon", "icon": "shield-check"},
            ],
        }
    )


def test_picture_rows_breathe_one_spacing_step_more_than_text_rows():
    """A list with a picture row spaces its rows one `--deck-space-*` step
    above the text-only list of the same density; a list without one keeps
    the base gaps."""
    css = PRESENTATION_CSS.read_text(encoding="utf-8")
    base = {
        "normal": r"\.deck-root \.deck-list \{[^}]*row-gap: var\(--deck-space-2\);",
        "compact": r'\.deck-root \.deck-list\[data-density="compact"\] \{ row-gap: var\(--deck-space-1\); \}',
        "tight": r'\.deck-root \.deck-list\[data-density="tight"\] \{ row-gap: 2px; \}',
    }
    for pattern in base.values():
        assert re.search(pattern, css)
    stepped = {"normal": "--deck-space-3", "compact": "--deck-space-2", "tight": "--deck-space-1"}
    for density, token in stepped.items():
        rule = (
            rf'\.deck-root \.deck-list\[data-row-logos="true"\]\[data-density="{density}"\] '
            rf"\{{ row-gap: var\({token}\); \}}"
        )
        assert re.search(rule, css), density
