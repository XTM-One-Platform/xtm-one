"""The Google Chat decision card render lab: [service.lab-google-chat-cards] (#842, S11).

The lab draws the checkout's own cardsV2 (``render_google_chat_decision``, ``link_message``) for every state a UX
review covers, so a card change is reviewed rendered. These pin the declaration, that every state renders in every
language, that the drawing escapes the ask's text and that the JSON page is the exact payload.
"""

from __future__ import annotations

import importlib.util
import json

import pytest

from app.services import google_chat_decision_cards as cards
from tests.test_lab_seed_declarations import CHECKS, LAB, _services

SCRIPT = LAB.parent / "google_chat_cards.py"

pytestmark = pytest.mark.skipif(not CHECKS.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_google_chat_cards", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_service_runs_the_script_on_the_backend_alone():
    service = _services()["lab-google-chat-cards"]
    module = _module()
    assert service["argv"] == ["python", ".xtm-one/lab/google_chat_cards.py"]
    assert service["image"] == "python-dev" and service["ready_path"] == "/lab/ready"
    assert service["port"] == module.PORT
    assert "backend" in service["needs"]


def test_the_port_never_meets_another_lab():
    module = _module()
    others = {spec["port"] for name, spec in _services().items() if name != "lab-google-chat-cards"}
    assert module.PORT not in others


def test_the_states_cover_every_source_with_and_without_the_optional_fields():
    states = set(_module().STATES)
    for source in ("hitl", "tool-approval", "flow", "work-item"):
        assert {f"open-{source}", f"open-{source}-bare"} <= states
    assert {
        "decided-option",
        "decided-text",
        "decided-tools",
        "expired",
        "cancelled",
        "refusal",
        "user-links",
    } <= states


@pytest.mark.parametrize("language", ["en", "es", "fr", "ja"])
def test_every_state_renders_and_draws_in_every_language(language):
    module = _module()
    for state in module.STATES:
        drawn = module.figure(state, language)
        assert f'id="{state}"' in drawn
        assert "w-unknown" not in drawn, state


def test_an_open_card_is_the_renderers_payload_and_its_buttons_are_drawn():
    module = _module()
    built = module.build("open-work-item", "en")
    assert built["kind"] == "message"
    assert built["payload"]["cardsV2"][0]["cardId"] == cards.CARD_ID
    drawn = module.figure("open-work-item", "en")
    assert 'class="btn filled"' in drawn and "Ship tonight" in drawn
    assert cards.ACTION_ANSWER in drawn and cards.ACTION_TEXT in drawn
    assert "<textarea" in drawn


def test_the_bare_card_has_no_recommendation_deadline_or_external_link():
    module = _module()
    payload = json.dumps(module.build("open-hitl-bare", "en")["payload"])
    assert '"color"' not in payload and "decoratedText" not in payload and "github.com" not in payload


def test_the_drawing_escapes_ask_text_and_keeps_chat_markup():
    module = _module()
    assert module.chat_html("<b>a</b> <script>x</script>") == "<b>a</b> &lt;script&gt;x&lt;/script&gt;"
    assert module.chat_html('<font color="#5f6368">t</font>') == '<span style="color:#5f6368">t</span>'
    assert module.chat_html("a &lt;b&gt;") == "a &lt;b&gt;"


def test_the_refusals_and_the_user_links_draw():
    module = _module()
    refusal = module.build("refusal", "ja")
    assert refusal["kind"] == "private" and len(refusal["payload"]["texts"]) == len(module.REFUSALS)
    assert "Only visible to you" in module.figure("refusal", "ja")
    links = module.build("user-links", "en")
    assert links["kind"] == "text" and "|Ship tonight>" in links["payload"]["text"]
    drawn = module.figure("user-links", "en")
    assert ">Ship tonight</a>" in drawn and ">http" not in drawn


def test_the_pages_answer_each_theme_and_width():
    module = _module()
    for theme in module.THEMES:
        for width, pixels in module.WIDTHS.items():
            body = module.page("t", module.figure("expired", "en"), theme, width).decode()
            assert f'class="{theme}"' in body and f"max-width:{pixels}px" in body
