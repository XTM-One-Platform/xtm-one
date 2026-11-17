"""The per-slide XTM One palette and the bento
content slide - the spec models, the validator's rules and the tool contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.builtin_tools.presentation import _EDIT_DESCRIPTION
from app.services.presentation_spec import SLIDE_PALETTES, slide_model


def _image(file_id: str = "00000000-0000-0000-0000-000000000001") -> dict:
    return {"type": "image", "file_id": file_id}


def _text(content: str = "Intel and posture in one graph.") -> dict:
    return {"type": "text", "content": content}


def _content(**fields: object) -> object:
    model = slide_model("content")
    assert model is not None
    return model.model_validate({"layout": "content", "title": "One investigation", **fields})


def test_palette_is_one_value_and_optional() -> None:
    assert SLIDE_PALETTES == ("xtm_one",)
    assert _content(blocks=[_text()]).palette is None
    assert _content(blocks=[_text()], palette="xtm_one").palette == "xtm_one"
    with pytest.raises(ValidationError):
        _content(blocks=[_text()], palette="violet")


@pytest.mark.parametrize("layout", ["title", "section", "agenda", "roadmap", "takeaways", "closing"])
def test_palette_is_common_to_every_layout(layout: str) -> None:
    model = slide_model(layout)
    assert model is not None
    assert "palette" in model.model_fields


def test_bento_defaults_off_and_accepts_one_image_plus_text_cells() -> None:
    assert _content(blocks=[_text()]).bento is False
    for cells in (2, 3, 4):
        slide = _content(bento=True, blocks=[_image(), *[_text() for _ in range(cells)]])
        assert slide.bento is True


@pytest.mark.parametrize(
    ("blocks", "message"),
    [
        ([_image(), _text()], "3-5 blocks"),
        ([_image(), *[_text() for _ in range(5)]], "3-5 blocks"),
        ([_text(), _text(), _text()], "1-3 image or video blocks"),
        ([_image(), _image(), _image(), _image(), _text()], "1-3 image or video blocks"),
    ],
)
def test_bento_refuses_what_is_no_bento(blocks: list[dict], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _content(bento=True, blocks=blocks)


def test_bento_refuses_columns() -> None:
    with pytest.raises(ValidationError, match="not columns"):
        _content(bento=True, columns=[[_image()], [_text()]])


def test_tool_contract_names_both_capabilities() -> None:
    assert "palette 'xtm_one'" in _EDIT_DESCRIPTION
    assert "bento: true" in _EDIT_DESCRIPTION
