"""An HTML agent reached by a chat transfer answers that chat in Markdown.

The web chat (and the OpenCTI chat panel, @filigran/chatbot) renders
Markdown with no raw HTML, so an ``html`` agent's answer, once a transfer
makes it the chat message, would show its tags. Its intent calls, consults
and background tasks keep HTML: their callers read it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.output_format import (
    answers_live_chat,
    build_output_format_prompt,
    chat_handover_output_format,
)

PERSONA = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "agents"
    / "seed_data"
    / "agent_persona"
    / "opencti-entity-insights.md"
)


@pytest.mark.parametrize("ho_type", ["transfer", "handover"])
def test_an_html_agent_answers_a_transferred_chat_in_markdown(ho_type):
    assert chat_handover_output_format("html", {"type": ho_type}) == "markdown"


def test_a_handover_with_no_type_is_a_chat_handover():
    assert chat_handover_output_format("html", {"source_agent": "x"}) == "markdown"


@pytest.mark.parametrize(
    ("fmt", "context"),
    [
        ("html", None),
        ("html", {}),
        ("html", {"type": "consult"}),
        ("html", {"type": "sub_agent_task"}),
        ("json", {"type": "transfer"}),
        ("markdown", {"type": "transfer"}),
        ("plain_text", {"type": "transfer"}),
    ],
)
def test_every_other_run_keeps_its_format(fmt, context):
    assert chat_handover_output_format(fmt, context) == fmt


@pytest.mark.parametrize("context", [None, {}, {"type": "transfer"}, {"type": "handover"}, {"source_agent": "x"}])
def test_a_direct_chat_a_transfer_and_a_handover_answer_a_live_chat(context):
    assert answers_live_chat(context)


@pytest.mark.parametrize("ho_type", ["consult", "sub_agent_task"])
def test_a_consult_and_a_background_task_answer_an_agent(ho_type):
    assert not answers_live_chat({"type": ho_type})


def test_the_transferred_run_is_told_markdown():
    prompt = build_output_format_prompt(chat_handover_output_format("html", {"type": "transfer"}))
    assert "Markdown" in prompt and "HTML markup" not in prompt


def test_the_entity_insights_persona_scopes_html_to_intent_calls():
    text = PERSONA.read_text(encoding="utf-8")
    rules = text.split("## Output Rules (all modes)", 1)[1]
    assert "For an intent call (cti.*), write in **HTML format only**" in rules
    assert "transferred a chat to you" in rules and "no `<meta>` tag" in rules
