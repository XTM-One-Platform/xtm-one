"""Renamed verticals and intent namespaces resolve to their current names.

An OpenCRQ build that predates the ``grc`` -> ``crq`` rename keeps
registering ``business_vertical: grc`` and ``grc.*`` intents, and keeps
reading ``GET /intents/catalog?vertical=grc``. Both boundaries resolve the
old names, so the old vertical and intents are never recreated.
"""

from __future__ import annotations

import inspect

from app.agents.builtin_agents import BUILTIN_AGENTS
from app.agents.seed import _EDITING_INTENT_AGENTS, _INTENT_DESCRIPTIONS
from app.api.v1 import intents as intents_api
from app.api.v1 import platform as platform_api
from app.services.intent_aliases import RENAMED_VERTICALS, canonical_intent_name, canonical_vertical


def test_a_renamed_vertical_resolves_to_its_current_name() -> None:
    assert canonical_vertical("grc") == "crq"
    assert canonical_vertical("crq") == "crq"
    assert canonical_vertical("cti") == "cti"
    assert canonical_vertical(None) is None
    assert canonical_vertical("") == ""


def test_an_intent_in_a_renamed_namespace_moves_with_it() -> None:
    assert canonical_intent_name("grc.control_gap_analysis") == "crq.control_gap_analysis"
    assert canonical_intent_name("grc.risk_scenario_generator") == "crq.risk_scenario_generator"
    assert canonical_intent_name("crq.assessment_summary") == "crq.assessment_summary"
    assert canonical_intent_name("global.assistant") == "global.assistant"
    assert canonical_intent_name("grcx.thing") == "grcx.thing"
    assert canonical_intent_name("grc") == "grc"


def test_no_builtin_names_a_former_vertical_or_namespace() -> None:
    former = set(RENAMED_VERTICALS)
    for agent in BUILTIN_AGENTS:
        intents = agent.get("supported_intents") or {}
        for name, config in intents.items() if isinstance(intents, dict) else ():
            assert name.partition(".")[0] not in former, (agent["slug"], name)
            if isinstance(config, dict):
                assert not former & set(config), (agent["slug"], name)
    assert not former & {vertical for _, vertical in _EDITING_INTENT_AGENTS}
    assert not any(name.partition(".")[0] in former for name in _INTENT_DESCRIPTIONS)


def test_the_opencrq_assistant_serves_the_crq_vertical() -> None:
    [agent] = [a for a in BUILTIN_AGENTS if a["slug"] == "opencrq-assistant"]
    intents = agent["supported_intents"]
    assert intents["global.assistant"] == {"crq": 5}
    for capability in ("control_gap_analysis", "risk_scenario_generator", "assessment_summary"):
        assert intents[f"crq.{capability}"] == {"crq": 0}


def test_registration_and_catalog_read_the_old_names_under_the_new_ones() -> None:
    """Both boundaries a platform crosses go through the aliases."""
    sync = inspect.getsource(platform_api._sync_platform_intents)
    assert "canonical_vertical(business_vertical)" in sync
    assert "canonical_intent_name(decl.name)" in sync
    assert "canonical_vertical(sent_vertical)" in inspect.getsource(platform_api._registered_business_vertical)
    catalog = inspect.getsource(intents_api.get_intent_catalog)
    assert "canonical_vertical(vertical)" in catalog
    assert "canonical_intent_name(" in catalog
