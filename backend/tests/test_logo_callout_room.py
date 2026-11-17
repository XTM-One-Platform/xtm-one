"""A logo grid that ends a `fill` callout is measured against the card.

A grid that does not ``fill`` packs its rows at the top of its field, so the
field is exactly as tall as its logos and the coverage audit read ~100% - while
the card around it, stretched by the callout's own ``fill``, left most of its
height bare under the logos. The page now measures such a grid against the room
it was given (down to the callout body's bottom) and says so (``room:
"callout"``), so the finding names the switches that actually apply. Every
other placement is measured exactly as before: the pixels are proven by the
layout-check E2E (``tests_e2e/presentation-layout-check.spec.ts``).
"""

from __future__ import annotations

from app.services.presentation_export import LAYOUT_AUDIT_JS
from app.services.presentation_layout import LOGO_MIN_COVERAGE, findings_from_observations


def _spec() -> dict:
    return {"title": "Deck", "slides": [{"layout": "content", "title": "Customers"}]}


def _observation(**extra) -> dict:
    return {"kind": "logo", "block": "logo-grid", "height": 48, "coverage": 0.12, "text": "Acme", **extra}


def _findings(observation: dict):
    return findings_from_observations(
        [{"index": 0, "slide_id": "s0", "layout": "content", "findings": [observation]}], _spec()
    )


def test_a_grid_lost_in_its_callout_is_told_to_fill_the_card_or_unstretch_it():
    (finding,) = _findings(_observation(room="callout"))
    assert finding.kind == "logo" and finding.severity == "advisory"
    assert f"cover 12% of their box (under {round(LOGO_MIN_COVERAGE * 100)}%)" in finding.message
    assert "set fill on the logo grid" in finding.message
    assert "drop fill on the callout" in finding.message
    # The grid's own rows are not the problem: it does not fill anything.
    assert "set rows" not in finding.message


def test_a_grid_measured_on_its_own_field_keeps_its_remedy():
    (finding,) = _findings(_observation())
    assert "set rows so the rows fill the box, or drop fill." in finding.message
    assert "callout" not in finding.message


def test_the_audit_measures_a_packed_grid_against_the_callout_it_ends():
    # Only a grid that does NOT fill, is the last block of a callout body, in a
    # callout that DOES fill: that is the one placement where the card's height
    # is room the logos were given and the field cannot see.
    assert "const calloutRoom = (root) => {" in LAYOUT_AUDIT_JS
    assert 'root.getAttribute("data-fill") === "true"' in LAYOUT_AUDIT_JS
    assert 'callout.getAttribute("data-fill") !== "true"' in LAYOUT_AUDIT_JS
    assert "lastShown(blocks) !== root" in LAYOUT_AUDIT_JS
    assert "lastShown(body) !== blocks" in LAYOUT_AUDIT_JS
    # The room reaches the body's inner bottom, minus whatever the grid draws
    # under its field (a caption), and is never smaller than the field itself.
    assert "Math.max(fr.height, room.bottom - fr.top - (rootRect.bottom - fr.bottom))" in LAYOUT_AUDIT_JS
    assert 'room: room ? "callout" : undefined' in LAYOUT_AUDIT_JS
