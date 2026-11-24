"""The Slack decision card render lab: [service.lab-slack-cards] (#842, S10).

The lab draws the checkout's own Block Kit (``render_slack_decision``, ``text_modal``) for every state a UX
review covers, so a card change is reviewed rendered. These pin the declaration, that every state renders in
every language, that the drawing escapes the ask's text and that the JSON page is the exact payload.
"""

from __future__ import annotations

import importlib.util
import json

import pytest

from app.services import slack_decision_cards as cards
from tests.test_lab_seed_declarations import CHECKS, LAB, _services

SCRIPT = LAB.parent / "slack_cards.py"

pytestmark = pytest.mark.skipif(not CHECKS.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_slack_cards", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_service_runs_the_script_on_the_backend_alone():
    service = _services()["lab-slack-cards"]
    module = _module()
    assert service["argv"] == ["python", ".xtm-one/lab/slack_cards.py"]
    assert service["image"] == "python-dev" and service["ready_path"] == "/lab/ready"
    assert service["port"] == module.PORT
    assert "backend" in service["needs"]


def test_the_port_never_meets_another_lab():
    module = _module()
    others = {spec["port"] for name, spec in _services().items() if name != "lab-slack-cards"}
    assert module.PORT not in others


def test_the_states_cover_every_source_with_and_without_the_optional_fields():
    states = set(_module().STATES)
    for source in ("hitl", "tool-approval", "flow", "work-item"):
        assert {f"open-{source}", f"open-{source}-bare"} <= states
    assert {"decided-option", "decided-text", "decided-tools", "expired", "cancelled", "modal", "refusal"} <= states


@pytest.mark.parametrize("language", ["en", "es", "fr", "ja"])
def test_every_state_renders_and_draws_in_every_language(language):
    module = _module()
    for state in module.STATES:
        drawn = module.figure(state, language)
        assert f'id="{state}"' in drawn
        assert "b-unknown" not in drawn, state


def test_an_open_card_is_the_renderers_payload_and_its_buttons_are_drawn():
    module = _module()
    built = module.build("open-work-item", "en")
    assert built["kind"] == "message"
    assert built["payload"]["blocks"][0]["type"] == "header"
    drawn = module.figure("open-work-item", "en")
    assert 'class="btn primary"' in drawn and "Ship tonight" in drawn
    assert 'class="date"' in drawn
    assert cards.ACTION_TEXT in drawn


def test_the_bare_card_has_no_recommendation_deadline_or_links():
    module = _module()
    blocks = json.dumps(module.build("open-hitl-bare", "en")["payload"]["blocks"])
    assert "<!date^" not in blocks and '"primary"' not in blocks and "github.com" not in blocks


def test_the_drawing_escapes_ask_text_and_keeps_slack_tokens():
    module = _module()
    assert module.mrkdwn("*a &lt;b&gt;* `c`") == "<b>a &lt;b&gt;</b> <code>c</code>"
    assert "<script" not in module.text_object({"type": "plain_text", "text": "<script>x</script>"})
    linked = module.mrkdwn("<https://example.com|Example>")
    assert linked == '<a href="https://example.com">Example</a>'


def test_the_modal_and_the_refusal_draw_over_the_open_card():
    module = _module()
    modal = module.build("modal", "fr")
    assert modal["kind"] == "modal" and modal["payload"]["callback_id"] == cards.MODAL_CALLBACK
    refusal = module.build("refusal", "ja")
    assert refusal["kind"] == "ephemeral" and len(refusal["payload"]["texts"]) == len(module.REFUSALS)
    assert "Only visible to you" in module.figure("refusal", "ja")


def test_the_pages_answer_each_theme_and_width():
    module = _module()
    for theme in module.THEMES:
        for width, pixels in module.WIDTHS.items():
            body = module.page("t", module.figure("expired", "en"), theme, width).decode()
            assert f'class="{theme}"' in body and f"max-width:{pixels}px" in body
