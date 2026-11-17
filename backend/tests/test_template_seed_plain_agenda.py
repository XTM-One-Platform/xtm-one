"""The plain agenda advertises no highlighted item.

The Filigran Brand Deck declares ``design.agenda: "plain"`` and the brand
team dropped the "current" agenda variant there (one item highlighted, the
others dimmed). The seed advertises its agenda entry without the ``current``
field and without a sentence about it; every other template keeps the full
catalog, and the spec still accepts ``current`` everywhere.
"""

from __future__ import annotations

from app.services.presentation_spec import layout_catalog, validate_spec
from app.services.template_container_seed import (
    BUILTIN_TEMPLATE_CONTAINERS,
    _build_layouts,
    _layouts_for_design,
)


def _agenda(entries: list[dict]) -> dict:
    return next(entry for entry in entries if entry["id"] == "agenda")


def _container(slug: str) -> dict:
    return next(c for c in BUILTIN_TEMPLATE_CONTAINERS if c["slug"] == slug)


def test_filigran_brand_agenda_offers_no_current() -> None:
    brand = _container("filigran-brand")
    layouts = _layouts_for_design(_build_layouts(), brand["extra_metadata"]["design"])
    agenda = _agenda(layouts)
    assert "current" not in agenda["fields"]
    assert "current" not in agenda["use_for"].lower()
    assert "highlight" not in agenda["use_for"].lower()
    # Every other field of the layout is still advertised.
    assert agenda["fields"] == [f for f in layout_catalog()["agenda"] if f != "current"]


def test_other_layouts_of_the_brand_deck_are_unchanged() -> None:
    brand = _container("filigran-brand")
    full = _build_layouts()
    layouts = _layouts_for_design(full, brand["extra_metadata"]["design"])
    assert [e for e in layouts if e["id"] != "agenda"] == [e for e in full if e["id"] != "agenda"]


def test_templates_without_the_plain_agenda_keep_current() -> None:
    full = _build_layouts()
    assert "current" in _agenda(full)["fields"]
    assert _layouts_for_design(full, None) is full
    assert _layouts_for_design(full, {"agenda": "cards"}) is full
    for container in BUILTIN_TEMPLATE_CONTAINERS:
        design = (container.get("extra_metadata") or {}).get("design")
        if container["slug"] == "filigran-brand" or not isinstance(design, dict):
            continue
        if design.get("agenda") != "plain":
            assert _layouts_for_design(full, design) is full


def test_the_spec_still_accepts_current() -> None:
    spec = validate_spec(
        {
            "title": "Deck",
            "slides": [
                {"layout": "agenda", "items": [{"title": "One"}, {"title": "Two"}, {"title": "Three"}], "current": 2}
            ],
        }
    )
    assert spec["slides"][0]["current"] == 2
