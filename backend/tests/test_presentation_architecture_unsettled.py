"""An architecture figure whose layout did not converge (#11580).

The renderer freezes a figure whose room kept changing (its drawing's ratio
and the slide's shrink-to-fit feeding back into each other) and publishes
``data-layout-unsettled``; the layout check reads it and names the figure,
instead of the render hanging until its timeout. Pinned here: the finding,
and the audit script reading the attribute. Every fixture is synthetic.
"""

from __future__ import annotations

from app.services.presentation_export import LAYOUT_AUDIT_JS
from app.services.presentation_layout import findings_from_observations
from tests.test_presentation_architecture_zone_chips import _block, _deck, _slide


def test_an_unsettled_figure_is_a_dense_finding_naming_the_figure_and_the_remedies():
    spec = _deck(_block({"label": "Platform"}))
    (finding,) = findings_from_observations(
        _slide([{"kind": "unsettled", "block": "architecture", "architecture": 2}]), spec
    )
    assert finding.kind == "unsettled"
    assert finding.severity == "dense"
    assert finding.message.startswith("architecture 2: its layout did not converge")
    assert "logo_height" in finding.message


def test_the_audit_reads_the_renderer_attribute():
    assert 'figure.dataset.layoutUnsettled === "true"' in LAYOUT_AUDIT_JS
    assert 'kind: "unsettled"' in LAYOUT_AUDIT_JS
