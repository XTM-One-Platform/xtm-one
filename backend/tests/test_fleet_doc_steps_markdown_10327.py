"""How it works carries a markdown body per step (#10327).

Pinned here: the step body holds up to STEP_DETAIL_MAX characters and is refused
past it; a legacy step without a body still validates; the generation prompt
asks for markdown of 25 to 40 words with member names in bold and bullets; the
one-page datasheet keeps the first sentence of the body as plain text.
"""

from __future__ import annotations

import pytest

from app.services import fleet_datasheet as sheet
from app.services.fleet_doc_generation import STRUCTURED_GUIDE
from app.services.fleet_docs import STEP_DETAIL_MAX, DocStep, clean_structured
from app.services.fleets import FleetActionRefusedError


def test_a_markdown_body_up_to_the_limit_is_kept():
    body = "**Assistant** reads the request and hands it over.\n\n- files a work item\n- posts to Teams"
    step = DocStep(title="Work enters through the Assistant", detail=body)
    assert step.detail == body
    assert STEP_DETAIL_MAX >= 800


def test_a_body_over_the_limit_is_refused():
    with pytest.raises(FleetActionRefusedError):
        clean_structured({"steps": [{"title": "Too long", "detail": "w" * (STEP_DETAIL_MAX + 1)}]})


def test_a_legacy_step_without_a_body_still_validates():
    cleaned = clean_structured({"steps": [{"title": "Assistant delegates"}]})
    assert cleaned == {"steps": [{"title": "Assistant delegates"}]}


def test_the_prompt_asks_for_markdown_of_25_to_40_words():
    assert "25 to 40 words" in STRUCTURED_GUIDE
    assert "**bold**" in STRUCTURED_GUIDE
    assert '"- " bullets' in STRUCTURED_GUIDE
    assert "3 to 10 steps" in STRUCTURED_GUIDE
    assert "no heading, no HTML" in STRUCTURED_GUIDE


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("**Assistant** reads the mail. Then it files a work item.", "Assistant reads the mail."),
        ("The *desk* acts:\n\n- files an item\n- posts", "The desk acts: files an item posts"),
        ("", ""),
    ],
)
def test_the_datasheet_keeps_the_first_sentence_as_plain_text(body, expected):
    assert sheet.first_sentence(body, 140) == expected
