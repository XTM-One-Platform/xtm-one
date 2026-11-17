"""The closing slide's ``align``.

Absent stays absent (a stored deck reads unchanged, the centred closing), and
``start`` / ``center`` are kept; anything else is refused like the title
slide's ``align``.
"""

from __future__ import annotations

import pytest

from app.services.presentation_spec import validate_spec


def _closing(**extra: object) -> dict:
    return {
        "title": "Closing align",
        "slides": [
            {"layout": "title", "title": "Cover"},
            {"layout": "closing", "title": "Thank you", **extra},
        ]
    }


def _stored_closing(**extra: object) -> dict:
    return validate_spec(_closing(**extra))["slides"][1]


def test_an_absent_align_stays_absent():
    assert "align" not in _stored_closing()


@pytest.mark.parametrize("align", ["start", "center"])
def test_a_closing_keeps_its_align(align: str):
    assert _stored_closing(align=align)["align"] == align


def test_an_unknown_closing_align_is_refused():
    with pytest.raises(ValueError):
        validate_spec(_closing(align="end"))
