"""The weekly threat sweep records its batch durably (#1837, gap ctem-loop-sweep-batch-no-durable-record).

The sweep's relevant threats must not live only in the run's conversation: the seeded
prompt records them in one ``record_ctem_signals`` call (one signal row each, #1726),
and the loop orchestrator that runs the sweep is offered that tool.
"""

from __future__ import annotations

from app.agents import builtin_agents as ba
from app.services.builtin_tools.ctem_signals import (
    CTEM_SIGNAL_AGENT_SLUGS,
    CTEM_SIGNAL_TOOL_NAMES,
)


def _sweep() -> dict:
    return next(a for a in ba.CTEM_F1_ASSIGNMENTS if a["name"] == "Weekly threat sweep")


def test_sweep_prompt_records_the_batch_in_one_bulk_signal_call() -> None:
    prompt = _sweep()["prompt"]
    assert "record_ctem_signals" in prompt
    assert "one record_ctem_signals call" in prompt
    assert "relevant_threat" in prompt


def test_sweep_runner_is_offered_the_bulk_signal_tool() -> None:
    assert "record_ctem_signals" in CTEM_SIGNAL_TOOL_NAMES
    assert "ctem-threat-response-orchestrator" in CTEM_SIGNAL_AGENT_SLUGS
