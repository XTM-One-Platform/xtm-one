"""``logo_height`` on an architecture node.

A card's or band's logo can be sized on the ``logo_grid`` / timeline steps
(sm 28 / md 40 / lg 56 / xl 72 canvas px). Pinned here: the contract (kept
when set, absent when unset so stored decks do not change), the refusals of a
height that would change nothing, and the hint the agent reads in the spec
reference. Every fixture is synthetic, its file ids placeholders.
"""

from __future__ import annotations

import pytest

from app.services.presentation_spec import LOGO_HEIGHT_PX, PresentationSpecError, spec_reference, validate_spec

NODE_LOGO = "00000000-0000-4000-8000-00000000b002"


def _deck(nodes: list[dict]) -> dict:
    block = {"type": "architecture", "nodes": nodes, "edges": [{"source": nodes[0]["id"], "target": nodes[1]["id"]}]}
    return {"title": "Platform", "slides": [{"layout": "content", "blocks": [block]}]}


def _other() -> dict:
    return {"id": "z", "label": "Other", "icon": "shield", "row": 2, "col": 1}


def _stored(node: dict) -> dict:
    return validate_spec(_deck([node, _other()]))["slides"][0]["blocks"][0]["nodes"][0]


def _refused(node: dict) -> str:
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(_deck([node, _other()]))
    return str(exc.value)


@pytest.mark.parametrize("height", sorted(LOGO_HEIGHT_PX))
@pytest.mark.parametrize("span", [1, 2])
def test_a_card_or_band_logo_keeps_its_height(height: str, span: int):
    node = {
        "id": "a",
        "label": "Vendor",
        "logo_file_id": NODE_LOGO,
        "logo_height": height,
        "row": 1,
        "col": 1,
        "col_span": span,
    }
    assert _stored(node)["logo_height"] == height


def test_unset_logo_height_is_not_stored():
    stored = _stored({"id": "a", "label": "Vendor", "logo_file_id": NODE_LOGO, "row": 1, "col": 1})
    assert stored.get("logo_height") is None


def test_logo_height_without_a_logo_is_refused():
    message = _refused({"id": "a", "label": "Vendor", "icon": "shield", "logo_height": "lg", "row": 1, "col": 1})
    assert "logo_height sizes a node's logo" in message


@pytest.mark.parametrize("shape", ["pill", "circle"])
def test_logo_height_on_a_pill_or_circle_is_refused(shape: str):
    node = {
        "id": "a",
        "label": "Vendor",
        "shape": shape,
        "logo_file_id": NODE_LOGO,
        "logo_height": "lg",
        "row": 1,
        "col": 1,
    }
    assert "logo_height applies to a card" in _refused(node)


def test_an_unknown_step_is_refused():
    node = {"id": "a", "label": "Vendor", "logo_file_id": NODE_LOGO, "logo_height": "xxl", "row": 1, "col": 1}
    assert "logo_height" in _refused(node)


def test_the_reference_names_the_field():
    assert "logo_height" in spec_reference()
