# SPDX-License-Identifier: Apache-2.0
"""The CTEM validation decision offers a launch scope (#2482, Sam, 26 Nov 2026).

Pinned without a database:
- the orchestrators that launch a scenario on asset groups offer the default
  scope (the whole group, listed first and recommended) and a sample of a few
  assets, launched through ``manage_openaev_scenario`` ``asset_ids``;
- the Gate answered prompt launches the scope the answer chose, a plain approve
  being the default (no breaking change for an existing decision);
- autonomous: the validation gate files no decision and the re-test prompts
  launch the default scope when the gate runs.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agents.builtin_agents import CTEM_F1_ASSIGNMENTS, CTEM_F2_ASSIGNMENTS
from app.agents.builtin_fleet_docs import BUILTIN_FLEET_STRUCTURED, CTEM_VALIDATION_GATE
from app.agents.seed import SEED_DATA_DIR
from app.services.fleet_autonomy import waiting_gate_keys

_ORCHESTRATORS = ("ctem-threat-response-orchestrator", "ctem-vuln-prioritization-orchestrator")
_FLEETS = ("ctem-loop", "ctem-vuln-prioritization")


def _persona(name: str) -> str:
    return (SEED_DATA_DIR / "agent_persona" / f"{name}.md").read_text()


def _assignment(assignments: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(a for a in assignments if a["name"] == name)


@pytest.mark.parametrize("persona", _ORCHESTRATORS)
def test_the_orchestrators_offer_default_first_and_recommended_then_a_sample(persona: str) -> None:
    text = _persona(persona)
    assert "two scopes, default first and always the recommended one" in text
    default = text.index("**Default**, the scenario's own targets, the whole asset group(s)")
    sample = text.index("**Sample**, a few assets of the group")
    assert default < sample
    assert "`launch` without `asset_ids`" in text and "`launch` with `asset_ids`" in text
    assert "the result measures only those assets" in text
    assert "never more than the group holds" in text
    assert "the case's own asset always included when the case names one" in text
    assert "The person may name the assets in their own words" in text
    assert "keeps it" in text  # the daily repeat keeps the scope chosen
    assert '"does not close the case"' in text
    assert "never widen by yourself" in text
    assert (
        'always launches the default scope, written on the case as passed automatically with scope "default (whole group)"'
        in text
    )


@pytest.mark.parametrize("assignments", [CTEM_F1_ASSIGNMENTS, CTEM_F2_ASSIGNMENTS])
def test_gate_answered_launches_the_chosen_scope_and_a_plain_approve_is_the_default(
    assignments: list[dict[str, Any]],
) -> None:
    prompt = _assignment(assignments, "Gate answered")["prompt"]
    assert "a plain approve or the default option launches the default scope" in prompt
    assert "manage_openaev_scenario launch without asset_ids" in prompt
    assert "launch with those asset_ids, a copy of the scenario, the original unchanged" in prompt
    assert "write the scope, the assets and the scenario launched on the case" in prompt


@pytest.mark.parametrize("assignments", [CTEM_F1_ASSIGNMENTS, CTEM_F2_ASSIGNMENTS])
def test_the_retest_decision_lists_the_default_scope_first_and_a_running_gate_launches_it(
    assignments: list[dict[str, Any]],
) -> None:
    prompt = _assignment(assignments, "Re-test due")["prompt"]
    assert prompt.index("the default scope first and recommended") < prompt.index("sample of")
    assert "launch it now on the default scope" in prompt


@pytest.mark.parametrize("slug", _FLEETS)
def test_autonomous_files_no_validation_decision(slug: str) -> None:
    """Autonomous: the gate runs, so no sample decision is filed and the default scope launches."""
    gates = BUILTIN_FLEET_STRUCTURED[slug]["gates"]
    assert CTEM_VALIDATION_GATE not in waiting_gate_keys("autonomous", gates)
    assert CTEM_VALIDATION_GATE in waiting_gate_keys("full_gates", gates)
