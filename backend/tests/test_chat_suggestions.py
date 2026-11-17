"""Suggested opening actions for the chat welcome screen.

Covers the pure parts of ``app.services.chat_suggestions``: no generic set
stands in for an agent's own suggestions, and the cleaning applied to an
agent's stored ``web_suggested_prompts`` — a free-form JSON column filled by
an LLM generation endpoint, so it is not guaranteed to hold a clean list of
strings.
"""

import asyncio
from unittest.mock import MagicMock

import app.services.chat_suggestions as chat_suggestions
from app.services.chat_suggestions import MAX_SUGGESTIONS, _clean, resolve_chat_suggestions


def test_no_generic_set_stands_in_for_the_agent():
    # The welcome screen showed "Summarize my unread emails" under agents that
    # never offered it: only the agent's own prompts are served now.
    assert not hasattr(chat_suggestions, "GENERIC_SUGGESTIONS")
    assert not hasattr(chat_suggestions, "generic_suggestions")


def test_no_agent_answers_no_suggestion():
    db = MagicMock()
    assert asyncio.run(resolve_chat_suggestions(db, MagicMock(), None, None)) == []
    db.execute.assert_not_called()


def test_clean_keeps_order_and_strips_whitespace():
    assert _clean(["  Assess my exposure  ", "Prioritise remediation"]) == [
        "Assess my exposure",
        "Prioritise remediation",
    ]


def test_clean_drops_blanks_duplicates_and_non_strings():
    raw = ["Assess my exposure", "   ", "Assess my exposure", 42, None, "Prioritise remediation"]
    assert _clean(raw) == ["Assess my exposure", "Prioritise remediation"]


def test_clean_caps_the_list():
    # The welcome screen lists these vertically under the greeting, so an agent
    # configured with twenty would push the composer off-screen.
    assert len(_clean([f"prompt {i}" for i in range(50)])) == MAX_SUGGESTIONS


def test_clean_rejects_non_list_payloads():
    for raw in (None, "a string", {"prompts": ["x"]}, 7):
        assert _clean(raw) == []
