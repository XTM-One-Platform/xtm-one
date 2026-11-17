"""The native ``positioning_map`` block (#11601).

Pinned here: the contract (axes, quadrants, items by x / y, callouts and the
per-item note), the layout-check findings the renderer feeds (labels that
found no free side, a frozen plot) and the audit script reading the
renderer's attributes. Every fixture is synthetic, its vendors fictional.
"""

from __future__ import annotations

import pytest

from app.services.presentation_export import LAYOUT_AUDIT_JS
from app.services.presentation_layout import findings_from_observations
from app.services.presentation_spec import (
    MAX_POSITIONING_MAP_CALLOUTS,
    MAX_POSITIONING_MAP_NOTE_CHARS,
    PresentationSpecError,
    logo_file_ids,
    validate_spec,
)


def _map(**extra) -> dict:
    return {
        "type": "positioning_map",
        "x_axis": {
            "title": "Coverage: single stage to whole loop",
            "low_label": "Single stage",
            "high_label": "Whole loop",
        },
        "y_axis": {"title": "Openness: closed to open", "low_label": "Closed", "high_label": "Open"},
        "quadrants": {"top_right": {"label": "Open platform", "tone": "accent"}},
        "items": [
            {"label": "Acme", "x": 90, "y": 92, "emphasis": "highlight"},
            {"label": "Northwind", "x": 82, "y": 80, "note": "Broad intel, partial validation"},
        ],
        **extra,
    }


def _deck(block: dict) -> dict:
    return {"title": "Landscape", "slides": [{"layout": "content", "blocks": [block]}]}


def _stored(block: dict) -> dict:
    return validate_spec(_deck(block))["slides"][0]["blocks"][0]


def _slide(findings: list[dict]) -> list[dict]:
    return [{"index": 0, "slide_id": "s1", "layout": "content", "findings": findings}]


def test_a_map_stores_its_items_at_their_coordinates_with_their_note():
    stored = _stored(_map())
    assert [(item["x"], item["y"]) for item in stored["items"]] == [(90, 92), (82, 80)]
    assert stored["items"][1]["note"] == "Broad intel, partial validation"
    assert stored["callouts"] == []
    assert stored["callouts_position"] == "below"


def test_the_logos_of_a_map_are_read_for_their_geometry():
    # Review thread on #11604: the asset_meta loader reads every logo a deck draws.
    block = _map()
    block["items"][0]["logo_file_id"] = "logo-acme"
    block["items"][1]["logo_file_id"] = "logo-northwind"
    assert logo_file_ids(_deck(block)) == ["logo-acme", "logo-northwind"]


def test_callouts_say_why_the_highlight_sits_top_right():
    stored = _stored(
        _map(
            callouts=[{"title": "Whole loop", "text": "One platform from intel to validation"}],
            callouts_position="right",
        )
    )
    assert stored["callouts"][0]["title"] == "Whole loop"
    assert stored["callouts_position"] == "right"


def test_more_than_four_callouts_are_refused():
    callouts = [{"title": f"Point {i}"} for i in range(MAX_POSITIONING_MAP_CALLOUTS + 1)]
    with pytest.raises(PresentationSpecError):
        validate_spec(_deck(_map(callouts=callouts)))


def test_a_long_note_is_refused():
    block = _map()
    block["items"][1]["note"] = "x" * (MAX_POSITIONING_MAP_NOTE_CHARS + 1)
    with pytest.raises(PresentationSpecError):
        validate_spec(_deck(block))


def test_a_coordinate_past_100_is_refused():
    block = _map()
    block["items"][0]["x"] = 101
    with pytest.raises(PresentationSpecError):
        validate_spec(_deck(block))


def test_overlapping_labels_are_a_dense_finding_naming_them():
    spec = _deck(_map())
    (finding,) = findings_from_observations(
        _slide(
            [
                {
                    "kind": "map_overlap",
                    "block": "positioning_map",
                    "map": 2,
                    "amount": 2,
                    "labels": ["Acme", "Northwind"],
                }
            ]
        ),
        spec,
    )
    assert finding.kind == "map_overlap"
    assert finding.severity == "dense"
    assert finding.amount == 2
    assert finding.message.startswith("positioning_map 2: 2 label(s) ('Acme', 'Northwind')")


def test_no_overlap_is_no_finding():
    spec = _deck(_map())
    assert findings_from_observations(_slide([{"kind": "map_overlap", "map": 1, "amount": 0}]), spec) == []


def test_a_frozen_map_is_named_as_a_map():
    spec = _deck(_map())
    (finding,) = findings_from_observations(_slide([{"kind": "unsettled", "block": "positioning_map", "map": 1}]), spec)
    assert finding.kind == "unsettled"
    assert finding.message.startswith("positioning_map 1:")


def test_the_audit_reads_the_renderer_attributes():
    assert "figure.dataset.mapOverlaps" in LAYOUT_AUDIT_JS
    assert 'kind: "map_overlap"' in LAYOUT_AUDIT_JS
    assert 'block: "positioning_map", map: position + 1' in LAYOUT_AUDIT_JS


def test_the_audit_measures_quadrant_and_end_labels_against_marks():
    """UX round 1 B4: a quadrant label under a mark, or an end label over a quadrant label, is a finding."""
    assert '".deck-map-label, .deck-map-quadrant-label, .deck-map-end"' in LAYOUT_AUDIT_JS
    assert 'figure.querySelectorAll(".deck-map-mark")' in LAYOUT_AUDIT_JS
    assert "Math.max(Number(figure.dataset.mapOverlaps) || 0, crowded.size)" in LAYOUT_AUDIT_JS
