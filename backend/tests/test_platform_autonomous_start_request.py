"""Regression tests for the ``AutonomousRunStartRequest`` boundary validator.

The pre-release autonomous audit found that ``author_scenario`` and ``plan_mode``
were accepted independently by the XTM One start API, while every downstream branch
keys off ``plan_mode`` alone: the engagement deadline
(``engagement_deadline_seconds``), the "do not execute" cycle instruction, and the
settle-to-PLANNED backstop. An ``author_scenario=True, plan_mode=False`` request would
therefore get the 7-day LIVE deadline and settle to COMPLETED instead of PLANNED.

OpenAEV never sends the flags apart - it DERIVES author mode from the other two
(``authorScenario = planMode && !hasText(simulationId)``) - but the API is a trust
boundary that accepts them independently, so the shape is re-established here rather
than assumed. These tests pin:

1. author-scenario requires a ``scenario_id`` (the scenario IS the target),
2. author-scenario rejects a ``simulation_id`` (author mode has no simulation, and a
   hybrid request would silently drop the author framing in the cycle prompt), and
3. author-scenario coerces ``plan_mode`` on, so build semantics can never diverge,

while leaving a normal (non-author) request's ``plan_mode`` exactly as sent.
"""

import pytest
from pydantic import ValidationError

from app.api.v1.platform import AutonomousRunStartRequest


def _base_kwargs(**overrides):
    kwargs = {"objective": "map the path", "openaev_run_id": "run-1"}
    kwargs.update(overrides)
    return kwargs


class TestAuthorScenarioRequiresScenarioId:
    def test_author_scenario_without_scenario_id_is_rejected(self):
        with pytest.raises(ValidationError, match="scenario_id is required"):
            AutonomousRunStartRequest(**_base_kwargs(author_scenario=True))

    def test_author_scenario_with_blank_scenario_id_is_rejected(self):
        with pytest.raises(ValidationError, match="scenario_id is required"):
            AutonomousRunStartRequest(
                **_base_kwargs(author_scenario=True, scenario_id="   ")
            )


class TestAuthorScenarioRejectsSimulation:
    def test_author_scenario_with_simulation_id_is_rejected(self):
        # Author mode is DEFINED by the absence of a simulation (OpenAEV derives it as
        # plan_mode AND no simulation), and _build_cycle_prompt resolves author mode as
        # "author_scenario and not simulation_id" - so a hybrid request would silently
        # drop the author framing and drive a build against a real simulation.
        with pytest.raises(ValidationError, match="simulation_id must not be set"):
            AutonomousRunStartRequest(
                **_base_kwargs(
                    author_scenario=True, scenario_id="scn-1", simulation_id="sim-1"
                )
            )

    def test_author_scenario_with_blank_simulation_id_is_accepted(self):
        # A blank/whitespace simulation id is "no simulation", not a hybrid: it must be
        # treated exactly like the omitted field so a sloppy caller is not rejected.
        req = AutonomousRunStartRequest(
            **_base_kwargs(author_scenario=True, scenario_id="scn-1", simulation_id="  ")
        )
        assert req.plan_mode is True


class TestAuthorScenarioImpliesPlanMode:
    def test_author_scenario_coerces_plan_mode_on_when_unset(self):
        req = AutonomousRunStartRequest(
            **_base_kwargs(author_scenario=True, scenario_id="scn-1")
        )
        assert req.plan_mode is True

    def test_author_scenario_coerces_plan_mode_on_when_explicitly_false(self):
        # The divergent combination the audit flagged: author mode with plan_mode
        # left off would otherwise get the LIVE deadline and settle to COMPLETED.
        req = AutonomousRunStartRequest(
            **_base_kwargs(author_scenario=True, scenario_id="scn-1", plan_mode=False)
        )
        assert req.plan_mode is True

    def test_openaev_normal_shape_is_idempotent(self):
        # What OpenAEV actually sends: both flags on together.
        req = AutonomousRunStartRequest(
            **_base_kwargs(author_scenario=True, scenario_id="scn-1", plan_mode=True)
        )
        assert req.plan_mode is True
        assert req.author_scenario is True


class TestNonAuthorRequestUntouched:
    def test_live_run_plan_mode_stays_false(self):
        # A live run (no author_scenario) must not be silently promoted to a build.
        req = AutonomousRunStartRequest(
            **_base_kwargs(simulation_id="sim-1", plan_mode=False)
        )
        assert req.plan_mode is False
        assert req.author_scenario is False

    def test_dry_run_plan_mode_without_author_is_preserved(self):
        # Legacy BUILD LOGIC: plan_mode with a throwaway simulation and no
        # author_scenario is still a valid, supported shape and must be left as-is.
        req = AutonomousRunStartRequest(
            **_base_kwargs(simulation_id="sim-1", plan_mode=True)
        )
        assert req.plan_mode is True
        assert req.author_scenario is False
