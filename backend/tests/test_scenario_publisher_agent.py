"""Contract tests for the OpenAEV Scenario Publisher built-in agent.

Pins the scenario pipeline's last mile: a lab-tested scenario on the author tenant becomes
a draft pull request on the ``scenarios`` catalogue repo. The wiring that makes
this reachable and safe is easy to drop silently, so it is pinned here:

- the Publisher persona must resolve and keep its guardrails (it never runs
  ``scenario-tools``; it exports the scenario, commits the export and the intake
  request under ``.scenario-publish/``, and opens a draft PR), otherwise the
  agent could try to author catalogue entries by hand — which cannot produce the
  repo's canonical normalisation;
- the Scenario Creator must hand control to the Publisher (``transfer``),
  otherwise a built scenario never reaches a PR;
- the Publisher binds the ``github`` integration, its door to the repo.
"""

from __future__ import annotations

import pytest

from app.agents.builtin_agents import BUILTIN_AGENTS
from app.agents.seed import _resolve_seed_content

_PUBLISHER_SLUG = "openaev-scenario-publisher"
_CREATOR_SLUG = "openaev-scenario-creator"


def _agent(slug: str) -> dict:
    for agent in BUILTIN_AGENTS:
        if agent.get("slug") == slug:
            return agent
    raise AssertionError(f"built-in agent {slug!r} not found in BUILTIN_AGENTS")


@pytest.fixture(scope="module")
def publisher() -> dict:
    return _agent(_PUBLISHER_SLUG)


class TestScenarioPublisher:
    def test_persona_resolves_from_seed_data(self, publisher):
        persona = _resolve_seed_content(
            "agent_persona", publisher.get("persona", _PUBLISHER_SLUG)
        )
        assert persona, (
            f"{_PUBLISHER_SLUG} persona must resolve to a seed file under "
            "seed_data/agent_persona/"
        )

    def test_persona_keeps_its_guardrails(self, publisher):
        persona = _resolve_seed_content("agent_persona", _PUBLISHER_SLUG)
        # It commits the export + request for the Action; it never builds the entry.
        assert ".scenario-publish/export.zip" in persona
        assert ".scenario-publish/request.yaml" in persona
        assert "export_openaev_scenario" in persona
        assert "scenario-tools" in persona  # named only to say it never runs it
        assert "draft" in persona.lower()

    def test_persona_exports_with_neutralised_targets(self, publisher):
        # The lab keeps its real target; what reaches the public catalogue names only
        # example hosts, so every export the Publisher commits is a neutralised one.
        persona = _resolve_seed_content("agent_persona", _PUBLISHER_SLUG)
        step2 = persona.split("### Step 2", 1)[1].split("### Step 3", 1)[0]
        assert "export_openaev_scenario" in step2
        assert "`neutralise_targets: true`" in step2
        assert "Never commit an export produced without `neutralise_targets: true`" in persona

    def test_binds_openaev_and_github(self, publisher):
        assert publisher["integration_template_bindings"] == ["openaev", "github"], (
            f"{_PUBLISHER_SLUG} exports the scenario (openaev) and commits it + "
            "opens the catalogue PR (github)"
        )

    def test_is_a_transfer_target_kept_out_of_the_picker(self, publisher):
        # Reached by transfer from the Scenario Creator; handovers never read
        # disable_chat, so the transferred conversation still answers.
        assert publisher["disable_chat"] is True
        assert publisher["output_format"] == "markdown"

    def test_reachable_from_the_scenario_creator(self):
        creator = _agent(_CREATOR_SLUG)
        assert _PUBLISHER_SLUG in creator["handover_agent_slugs"], (
            "the Scenario Creator must hand off to the Publisher, else a built "
            "scenario never reaches a pull request"
        )
        assert creator["handover_type_slugs"][_PUBLISHER_SLUG] == "transfer"

    def test_handover_targets_resolve(self, publisher):
        slugs = {a["slug"] for a in BUILTIN_AGENTS}
        for target in publisher.get("handover_agent_slugs", []):
            assert target in slugs, f"unknown handover target {target!r}"
        for key in publisher.get("handover_type_slugs", {}):
            assert key in publisher["handover_agent_slugs"]
