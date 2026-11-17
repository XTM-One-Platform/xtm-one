"""Tests for the handover-context system-prompt helper.

After a transfer the target agent is re-invoked with the user's ORIGINAL
message as the message to answer.  When that message is itself the routing
request ("transfer me to the X assistant"), the old context line ("Transferred
from <source> — this conversation has been handed over to you.") gave the
target LLM no hint that the request was already fulfilled, so it consistently
replied "you're already connected — no additional transfer is needed" instead
of taking over.  The helper now states explicitly that the handover is already
complete and forbids that meta-answer.
"""

from __future__ import annotations

from app.services.builtin_tools import format_handover_context_prompt


def _joined(context: dict) -> str:
    return "\n".join(format_handover_context_prompt(context))


# ── Transfer semantics ───────────────────────────────────────────────────────


def test_transfer_states_handover_already_complete():
    text = _joined(
        {
            "type": "transfer",
            "source_agent": '"CTEM Assistant" (id: abc)',
            "original_question": "Transfer me to the opencti assistant",
        }
    )
    assert "Transferred from" in text
    assert "CTEM Assistant" in text
    assert "ALREADY COMPLETE" in text
    # The critical anti-"already connected" directive.
    assert "never reply that a transfer is unnecessary" in text
    assert "already connected" in text
    # The user's message stays available as context.
    assert "Original question: Transfer me to the opencti assistant" in text


def test_transfer_includes_reason_when_present():
    text = _joined(
        {
            "type": "transfer",
            "source_agent": "Agent A",
            "reason": "User requested the OpenCTI Assistant.",
        }
    )
    assert "Reason: User requested the OpenCTI Assistant." in text


def test_generic_handover_gets_same_completion_semantics():
    text = _joined({"type": "handover", "source_agent": "Agent A"})
    assert "Handed over from Agent A" in text
    assert "ALREADY COMPLETE" in text
    assert "never reply that a transfer is unnecessary" in text


def test_unknown_type_falls_back_to_generic_handover():
    text = _joined({"source_agent": "Agent A"})
    assert "Handed over from Agent A" in text
    assert "ALREADY COMPLETE" in text


# ── Consult semantics (unchanged) ────────────────────────────────────────────


def test_consult_context_unchanged():
    text = _joined(
        {
            "type": "consult",
            "source_agent": "Agent A",
            "original_question": "What is APT28?",
        }
    )
    assert "Consulted by Agent A" in text
    assert "your response goes back to that agent" in text
    assert "Original question: What is APT28?" in text
    # Consults must NOT carry the transfer-completion directive.
    assert "ALREADY COMPLETE" not in text


# ── Background task semantics ────────────────────────────────────────────────


def test_sub_agent_task_gets_task_semantics_not_completion_directive():
    text = _joined(
        {
            "type": "sub_agent_task",
            "source_agent": "background task",
            "original_question": "Summarize the last 10 CVEs.",
        }
    )
    assert "delegated background task" in text
    assert "goes back to the delegating agent" in text
    assert "Original question: Summarize the last 10 CVEs." in text
    # A background worker has no live user to take over from or to greet —
    # the transfer-completion directive must NOT leak into task contexts.
    assert "ALREADY COMPLETE" not in text
    assert "ask what they need" not in text


# ── Source agent response block ──────────────────────────────────────────────


def test_source_agent_response_block_appended():
    fragments = format_handover_context_prompt(
        {
            "type": "transfer",
            "source_agent": "Agent A",
            "source_agent_response": "Here is what I found so far.",
        }
    )
    assert len(fragments) == 2
    assert "── Source Agent Response ──" in fragments[1]
    assert "Here is what I found so far." in fragments[1]
    assert "before transferring to you" in fragments[1]
    assert "do NOT repeat it verbatim" in fragments[1]


def test_source_response_verb_matches_generic_handover_mode():
    fragments = format_handover_context_prompt(
        {
            "type": "handover",
            "source_agent": "Agent A",
            "source_agent_response": "Partial findings.",
        }
    )
    assert len(fragments) == 2
    # The block's verb must match the "Handed over from ..." context line —
    # not hard-code "transferring".
    assert "before handing over to you" in fragments[1]
    assert "transferring" not in fragments[1]


def test_source_response_verb_matches_consult_mode():
    fragments = format_handover_context_prompt(
        {
            "type": "consult",
            "source_agent": "Agent A",
            "source_agent_response": "Context gathered so far.",
        }
    )
    assert len(fragments) == 2
    assert "before consulting you" in fragments[1]


def test_no_source_response_yields_single_fragment():
    fragments = format_handover_context_prompt(
        {"type": "transfer", "source_agent": "Agent A"}
    )
    assert len(fragments) == 1


def test_defaults_when_context_is_minimal():
    text = _joined({})
    assert "Handed over from another agent" in text
