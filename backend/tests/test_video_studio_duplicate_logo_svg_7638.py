"""The duplicate logo check reads a template's SVG brand mark.

* An SVG logo is drawn by the bounded rasteriser and matched as a picture: PIL never opens the SVG.
* A PNG logo is read as before.
* A mark that cannot be read is skipped with a note naming it; the other mark is still read.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from app.services.video_studio import render

_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60" viewBox="0 0 120 60">'
    b'<circle cx="30" cy="30" r="26" fill="#e63c28"/><rect x="64" y="14" width="52" height="32" fill="#1e5adc"/>'
    b"</svg>"
)


def _png(path: Path) -> str:
    Image.new("RGBA", (40, 20), (200, 40, 40, 255)).save(path)
    return str(path)


@pytest.mark.asyncio
async def test_an_svg_logo_is_drawn_before_the_match(tmp_path):
    logo = tmp_path / "logo.svg"
    logo.write_bytes(_SVG)
    marks, notes = await render._brand_mark_pictures({"logo": str(logo)})
    assert notes == []
    assert set(marks) == {"logo"}
    assert marks["logo"].mode == "RGBA"
    assert marks["logo"].size[0] > 0 and marks["logo"].getbbox() is not None


@pytest.mark.asyncio
async def test_an_svg_without_its_extension_is_still_drawn(tmp_path):
    logo = tmp_path / "logo"
    logo.write_bytes(b'<?xml version="1.0"?>\n' + _SVG)
    marks, notes = await render._brand_mark_pictures({"logo": str(logo)})
    assert notes == [] and set(marks) == {"logo"}


@pytest.mark.asyncio
async def test_a_png_logo_is_read_as_before(tmp_path):
    marks, notes = await render._brand_mark_pictures({"logo": _png(tmp_path / "logo.png")})
    assert notes == []
    assert marks["logo"].size == (40, 20)


@pytest.mark.asyncio
async def test_an_unreadable_mark_is_a_note_and_the_other_mark_is_read(tmp_path):
    broken = tmp_path / "mark.bin"
    broken.write_bytes(b"not a picture")
    marks, notes = await render._brand_mark_pictures({"logo": _png(tmp_path / "logo.png"), "mark": str(broken)})
    assert set(marks) == {"logo"}
    assert len(notes) == 1 and "mark" in notes[0] and "mark.bin" in notes[0]


@pytest.mark.asyncio
async def test_an_svg_the_rasteriser_draws_nothing_of_is_a_note(tmp_path, monkeypatch):
    from app.services.video_studio import screen_insert

    async def refuse(path: str, label: str = "") -> str:
        raise screen_insert.SvgNotDrawnError("refused for the test")

    monkeypatch.setattr(screen_insert, "svg_to_png", refuse)
    logo = tmp_path / "logo.svg"
    logo.write_bytes(_SVG)
    marks, notes = await render._brand_mark_pictures({"logo": str(logo)})
    assert marks == {}
    assert len(notes) == 1 and "logo.svg" in notes[0] and "refused for the test" in notes[0]
