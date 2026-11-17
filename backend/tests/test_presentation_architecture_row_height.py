"""One card height per architecture row: the layout check's ``arch_row`` finding.

The renderer centres every node on its grid row; the page probe groups the
framed cards whose shapes share a vertical centre into one ``arch_row``
observation with the spread between its tallest and shortest card, and the
layout check names a row whose spread is over ``ARCH_ROW_SPREAD_PX``. Pinned
here: the words, the threshold, and the hooks on both sides. Every fixture is
synthetic.
"""

from __future__ import annotations

from pathlib import Path

from app.services import presentation_export
from app.services.presentation_layout import ARCH_ROW_SPREAD_PX, findings_from_observations

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation"


def _deck() -> dict:
    block = {
        "type": "architecture",
        "nodes": [
            {"id": "a", "label": "Collect", "icon": "brain", "row": 1, "col": 1},
            {"id": "b", "label": "Enrich", "icon": "crosshair", "row": 1, "col": 2},
        ],
    }
    return {"title": "Pipeline", "slides": [{"layout": "content", "blocks": [block]}]}


def _slide(findings: list[dict]) -> list[dict]:
    return [{"index": 0, "slide_id": "s1", "layout": "content", "findings": findings}]


def test_a_row_of_cards_more_than_two_px_apart_is_an_advisory_naming_the_row():
    (finding,) = findings_from_observations(
        _slide(
            [{"kind": "arch_row", "block": "architecture", "architecture": 1, "row": 2, "amount": 18.4, "cards": 5}]
        ),
        _deck(),
    )
    assert finding.kind == "arch_row"
    assert finding.severity == "advisory"
    assert finding.amount == 18
    assert finding.message.startswith("architecture 1: the cards of row 2 differ in height by 18px")


def test_a_row_within_the_tolerance_says_nothing():
    assert ARCH_ROW_SPREAD_PX == 2
    for amount in (0, 1.5, 2):
        observation = {"kind": "arch_row", "architecture": 1, "row": 1, "amount": amount, "cards": 3}
        assert findings_from_observations(_slide([observation]), _deck()) == []


def test_a_row_mixing_unsized_logos_and_icon_tiles_names_logo_height_as_the_remedy():
    observation = {
        "kind": "arch_row", "architecture": 1, "row": 1, "amount": 8, "cards": 5, "unsized": 3, "tiles": 2,
    }
    (finding,) = findings_from_observations(_slide([observation]), _deck())
    assert "Set logo_height on its 3 logo cards" in finding.message
    assert "line counts" not in finding.message


def test_a_row_with_no_unsized_logo_keeps_the_text_remedy():
    observation = {"kind": "arch_row", "architecture": 1, "row": 1, "amount": 8, "cards": 4, "unsized": 0, "tiles": 4}
    (finding,) = findings_from_observations(_slide([observation]), _deck())
    assert "label and sublabel line counts" in finding.message
    assert "logo_height" not in finding.message


def test_the_probe_and_the_renderer_share_the_hooks():
    probe = Path(presentation_export.__file__).read_text(encoding="utf-8")
    assert '.deck-arch-node[data-shape="card"] > rect.deck-arch-node-shape' in probe
    assert 'kind: "arch_row"' in probe
    assert ".deck-arch-node-tile" in probe and ".deck-arch-logo-sized" in probe
    view = (FRONTEND / "blocks" / "Architecture.tsx").read_text(encoding="utf-8")
    assert 'className="deck-arch-node"' in view and "data-shape={node.kind}" in view
    assert 'className="deck-arch-node-shape"' in view
    assert 'className="deck-arch-node-tile"' in view and 'className="deck-arch-logo-sized"' in view
    assert 'className="deck-arch-logo"' in view
