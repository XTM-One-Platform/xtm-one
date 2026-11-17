"""Regression tests for the platform chat SSE status-event forwarding.

The contract is:

* ``status == "thinking_text"`` reasoning prose IS forwarded — the
  embedded chatbot in OpenCTI / OpenAEV renders it in a dedicated
  Cursor-style reasoning pane (small, dimmed, capped-height window),
  matching the native XTM One web chat UX.  Filtering it server-side
  (the previous behaviour) made the reasoning invisible to platform
  users while the model "thought out loud".
* Every other status event (``thinking`` iteration ticks,
  ``tool_start``, ``streaming``, ``transfer_start``, etc.) keeps
  flowing through so the UI retains its progress / tool visibility.
* Events missing the ``status`` key (or with an unrecognised value)
  are forwarded by default — the forwarding predicate is permissive
  about new event shapes so content is never silently dropped the
  moment a new event type is introduced upstream.
* The one exception is the agentic-action charge: it feeds the web
  chat's own quota counter, and the embedded chatbot has none.
"""

import pytest

from app.api.v1.platform import _should_forward_platform_status_event


def test_platform_chat_forwards_thinking_text_status_event():
    """Reasoning prose carrying ``status='thinking_text'`` MUST forward —
    the chatbot renders it in the dedicated reasoning pane.
    """
    assert _should_forward_platform_status_event(
        {"status": "thinking_text", "content": "model reasoning"}
    )


@pytest.mark.parametrize(
    "event",
    [
        {"status": "thinking", "iteration": 1},
        {"status": "streaming"},
        {"status": "tool_start", "tools": ["search"]},
        {"status": "transfer_start", "agent_name": "Helper"},
        {"status": "thinking_done", "iteration": 2},
    ],
)
def test_platform_chat_keeps_non_reasoning_status_events(event):
    """Every status event must round-trip the forwarding predicate so the
    UI keeps its progress / tool / transfer visibility.
    """
    assert _should_forward_platform_status_event(event)


@pytest.mark.parametrize(
    "event",
    [
        {"content": "data"},  # status key absent entirely
        {},  # empty dict
        {"type": "stream", "content": "chunk"},  # unrelated keys only
    ],
)
def test_platform_chat_forwards_events_with_missing_status_key(event):
    """Events that do not carry a ``status`` key must be forwarded by
    default — the predicate is intentionally permissive so a future
    contributor adding a new event shape upstream doesn't have to
    update an allowlist before content reaches the UI.
    """
    assert _should_forward_platform_status_event(event)


@pytest.mark.parametrize(
    "status_value",
    [
        "unknown_future_status",
        "",  # empty string
        "thinking-text",  # close-but-not-equal — kebab vs snake
    ],
)
def test_platform_chat_forwards_unknown_status_values(status_value):
    """Unknown / future status strings (including the empty string and
    near-miss variants) must forward by default.  Locking this prevents an
    accidental future flip to an opt-in allowlist that would silently
    swallow new status types introduced upstream.
    """
    assert _should_forward_platform_status_event({"status": status_value})


def test_platform_chat_keeps_the_agentic_action_charge_private():
    from app.services.agent_chat import AGENTIC_ACTION_STATUS

    assert not _should_forward_platform_status_event(
        {"status": AGENTIC_ACTION_STATUS, "actions": 1}
    )
