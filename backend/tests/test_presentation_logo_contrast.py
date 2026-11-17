"""A logo painted in nearly the colour of the surface under it is flagged.

A white one-colour brand mark placed in a block on a pale light slide was
there and unseen (#11579): the layout check measured logo heights and
coverage, never whether the mark could be seen at all. The page now reports
the lowest WCAG contrast ratio of a logo against what is painted under it,
and the audit grades it against ``LOGO_MIN_CONTRAST``.
"""

from __future__ import annotations

from typing import Any

from app.services.presentation_export import LAYOUT_AUDIT_JS
from app.services.presentation_layout import LOGO_MIN_CONTRAST, LayoutFinding, findings_from_observations

_SPEC = {"slides": [{"layout": "content", "title": "Architecture", "blocks": []}]}


def _findings(*observations: dict[str, Any]) -> list[LayoutFinding]:
    return findings_from_observations([{"index": 0, "layout": "content", "findings": list(observations)}], _SPEC)


def test_a_white_mark_on_a_pale_slide_is_flagged_dense() -> None:
    findings = _findings({"kind": "logo", "block": "architecture", "contrast": 1.07, "text": "XTM One"})
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "logo"
    assert finding.severity == "dense"
    assert finding.amount == 107
    assert "1.07:1" in finding.message
    assert '"XTM One"' in finding.message
    assert "logo_mark" in finding.message


def test_a_visible_mark_is_not_flagged() -> None:
    assert _findings({"kind": "logo", "block": "image", "contrast": LOGO_MIN_CONTRAST}) == []
    assert _findings({"kind": "logo", "block": "image", "contrast": 4.5}) == []


def test_a_contrast_observation_never_reads_as_a_height_or_coverage() -> None:
    # The contrast branch answers alone: no height or coverage grading on it.
    findings = _findings({"kind": "logo", "block": "logo-grid", "contrast": 3.2})
    assert findings == []


def test_the_page_measures_the_ink_against_the_surface_under_it() -> None:
    # The template's mark ink is read off the box that paints it; any other
    # mark off its own opaque pixels, on a canvas of its own (a tainted canvas
    # never poisons the colour reads).
    assert 'mark.getAttribute("data-brand-ink") === "true"' in LAYOUT_AUDIT_JS
    assert 'const sample = document.createElement("canvas");' in LAYOUT_AUDIT_JS
    # An architecture logo sits on its node's or zone's shape, never on the slide.
    assert 'node.querySelector(".deck-arch-node-shape")' in LAYOUT_AUDIT_JS
    assert 'zone.querySelector(".deck-arch-zone-panel")' in LAYOUT_AUDIT_JS
    # WCAG relative luminance and ratio.
    assert "0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b)" in LAYOUT_AUDIT_JS
    assert "(hi + 0.05) / (lo + 0.05)" in LAYOUT_AUDIT_JS
    assert 'kind: "logo", block: faint.block, contrast:' in LAYOUT_AUDIT_JS
