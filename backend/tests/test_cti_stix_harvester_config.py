"""Regression tests pinning the CTI STIX Harvester performance config.

The ``cti-stix-harvester`` built-in agent is tuned to cut
execution overhead: the four memory tiers are disabled (the workflow is a
stateless document-extraction task, so ``search_memory`` only adds an extra
planning + tool cycle), ``temperature`` is pinned to 0.0 for reproducible
extraction, and ``max_iterations`` is bounded to stop runaway repair loops.

The ``max_iterations`` value is coupled to the agent-loop wind-down
behaviour: tools are stripped starting at ``max_iterations - 2`` (see
``app/tools/agent_loop.py``), so the number of tool-enabled iterations is
``max_iterations - 2`` for ``max_iterations > 3``. The OpenCTI STIX bundle
construction skill allows up to three validator calls (one initial call plus
two repair revalidations), which needs at least three tool-enabled
iterations. A future edit that lowers ``max_iterations`` below 5 would
silently starve the validator and force the run to finalize without
validation - exactly the regression raised in the PR review. These tests pin
the contract so it is caught at CI time rather than in production.
"""

from __future__ import annotations

import pytest

from app.agents.builtin_agents import BUILTIN_AGENTS

_HARVESTER_SLUG = "cti-stix-harvester"

# Worst-case validator budget the STIX construction skill allows: one initial
# validation plus two repair revalidations (three ``stix_bundle_validator``
# calls total), each consuming one tool-enabled iteration.
_MIN_TOOL_ENABLED_ITERATIONS = 3


@pytest.fixture(scope="module")
def harvester() -> dict:
    for agent in BUILTIN_AGENTS:
        if agent.get("slug") == _HARVESTER_SLUG:
            return agent
    # Explicit raise (not ``pytest.fail``) so static analysis sees the
    # not-found path as non-returning instead of an implicit ``None`` return.
    raise AssertionError(
        f"built-in agent {_HARVESTER_SLUG!r} not found in BUILTIN_AGENTS"
    )


def _tool_enabled_iterations(max_iterations: int) -> int:
    """Mirror the tool-strip rule in ``app/tools/agent_loop.py``.

    Tools are stripped from ``strip_tools_at`` onward, so iterations
    ``0 .. strip_tools_at - 1`` retain tool access.
    """
    strip_tools_at = (
        max_iterations - 2 if max_iterations > 3 else max_iterations - 1
    )
    return max(strip_tools_at, 0)


class TestCtiStixHarvesterConfig:
    def test_all_memory_tiers_disabled(self, harvester):
        for field in (
            "memory_short_term_enabled",
            "memory_medium_term_enabled",
            "memory_persistent_enabled",
            "memory_user_enabled",
        ):
            assert harvester.get(field) is False, (
                f"{_HARVESTER_SLUG} must disable {field}: it is a stateless "
                "document-extraction task and memory adds an extra tool cycle"
            )

    def test_temperature_pinned_to_zero(self, harvester):
        assert harvester.get("temperature") == 0.0, (
            f"{_HARVESTER_SLUG} must pin temperature to 0.0 for reproducible "
            "STIX extraction"
        )

    def test_max_iterations_leaves_room_for_validator_budget(self, harvester):
        max_iterations = harvester.get("max_iterations")
        assert isinstance(max_iterations, int), (
            f"{_HARVESTER_SLUG} must set an explicit integer max_iterations"
        )
        tool_iterations = _tool_enabled_iterations(max_iterations)
        assert tool_iterations >= _MIN_TOOL_ENABLED_ITERATIONS, (
            f"{_HARVESTER_SLUG} max_iterations={max_iterations} leaves only "
            f"{tool_iterations} tool-enabled iteration(s); the STIX skill "
            f"needs at least {_MIN_TOOL_ENABLED_ITERATIONS} (initial "
            "validation + two repairs) before tools are stripped"
        )

    def test_validator_tool_is_bound(self, harvester):
        assert "stix_bundle_validator" in (
            harvester.get("builtin_tool_ids") or []
        ), (
            f"{_HARVESTER_SLUG} must bind the stix_bundle_validator builtin "
            "tool so the validation workflow can run"
        )
