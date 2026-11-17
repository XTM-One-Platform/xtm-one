"""The activation gate must see the same trigger context/payload as the run.

Regression test for the bug where a *gated* assignment skipped every run.
The activation gate only received the rendered activation template — and when
that template didn't explicitly reference ``{{trigger.*}}`` (e.g. "Is this a
new issue or an edited issue?"), the gate LLM had zero information about the
incoming event and answered ``SKIP: NO ISSUE CONTENT PROVIDED`` /
``SKIP: NO CONTEXT PROVIDED ABOUT WHAT "THIS"`` on every trigger.

The fix builds a single ``--- Trigger Information ---`` block from the run and
appends it to BOTH the activation-gate prompt and the main agentic prompt, so
the gate decides on the same data the run would act on.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from app.services import assignment_engine
from app.services.assignment_helpers import _build_trigger_information


def _make_run(*, trigger_context=None, trigger_payload=None):
    return SimpleNamespace(
        trigger_context=trigger_context,
        trigger_payload=trigger_payload,
    )


def test_build_trigger_information_includes_payload():
    run = _make_run(
        trigger_payload={
            "action": "opened",
            "issue": {"title": "Feature: add lat/long to city", "number": 6504},
        }
    )
    block = _build_trigger_information(run)
    assert "--- Trigger Information ---" in block
    assert "Trigger Payload:" in block
    assert "Feature: add lat/long to city" in block
    assert "opened" in block


def test_build_trigger_information_includes_context():
    run = _make_run(
        trigger_context={"trigger_type": "integration_event", "provider": "github"}
    )
    block = _build_trigger_information(run)
    assert "Trigger Context:" in block
    assert "github" in block


def test_build_trigger_information_strips_internal_keys():
    run = _make_run(
        trigger_context={
            "headers": {"x-github-event": "issues"},
            "body": {"action": "edited"},
            "provider": "github",
        },
        trigger_payload={
            "_webhook_meta": {"signature": "topsecret"},
            "action": "edited",
        },
    )
    block = _build_trigger_information(run)
    # Raw headers/body (context) and internal routing metadata (payload) are
    # stripped so the gate sees a clean, secret-free event view.
    assert "x-github-event" not in block
    assert "_webhook_meta" not in block
    assert "topsecret" not in block
    # Substantive fields survive.
    assert "github" in block
    assert "edited" in block


def test_build_trigger_information_empty_without_trigger_data():
    assert _build_trigger_information(_make_run()) == ""
    # Context/payload that contain only stripped keys yield no block.
    assert (
        _build_trigger_information(
            _make_run(trigger_context={"headers": {}, "body": {}})
        )
        == ""
    )
    assert (
        _build_trigger_information(_make_run(trigger_payload={"_webhook_meta": {}}))
        == ""
    )


def test_gate_receives_trigger_information_before_running():
    """``execute_assignment_run`` must append the trigger-information block to
    the gate prompt *before* calling ``_run_activation_gate``.

    Pinned via source inspection (same convention as
    ``test_agent_performance_and_fallbacks.py``) because the full execution
    path is too heavy to drive in a unit test. A regression that feeds the
    gate the bare ``rendered_activation`` again — re-introducing the
    "skips every gated run" bug — trips here.
    """
    source = inspect.getsource(assignment_engine.execute_assignment_run)

    # The shared block is built once from the run.
    assert "trigger_information = _build_trigger_information(run)" in source, (
        "execute_assignment_run must build the shared trigger-information "
        "block from the run."
    )

    gate_idx = source.index("rendered_activation = _render_prompt(")
    run_gate_idx = source.index("answer = await _run_activation_gate(")
    assert gate_idx < run_gate_idx, "fixture stale — gate call moved"

    # Between rendering the activation template and invoking the gate, the
    # trigger-information block MUST be appended to rendered_activation.
    between = source[gate_idx:run_gate_idx]
    assert "rendered_activation += trigger_information" in between, (
        "The activation gate must receive the trigger context/payload "
        "(rendered_activation += trigger_information) before running, "
        "otherwise a gate prompt without {{trigger.*}} is blind to the "
        "event and skips every run."
    )
