"""The exposure briefing records the sources it could not read (#2766).

A tool call of the Exposure Briefing Analyst that times out (``search_openaev_findings`` on a
large simulation answers "Retry narrower: inject_id.") used to vanish from the briefing while
``coverage.openaev`` stayed true. The seeded persona and prompt now retry once with the narrower
scope the error names, then record the source in the optional ``coverage.unread`` list and name
it at the end of the summary; the schema carries that list without breaking older briefings.
"""

from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError, validate

from app.agents.builtin_agents import (
    _EXPOSURE_BRIEFING_PERSONA,
    _EXPOSURE_BRIEFING_PROMPT,
    _EXPOSURE_BRIEFING_SCHEMA,
    BUILTIN_AGENTS,
    BUILTIN_ASSIGNMENTS,
)
from app.agents.seed import _resolve_seed_schema


def _briefing(**coverage: Any) -> dict[str, Any]:
    return {
        "headline": "Admin hostnames exposed by DNS enumeration",
        "summary": "Two exposures lead. Simulation Q4 external could not be read.",
        "posture_trend": "stable",
        "coverage": {"opencti": True, "openaev": True, **coverage},
        "key_findings": [{"title": "Exposed admin host", "severity": "high", "source": "openaev"}],
        "recommended_focus": "Close the admin hosts.",
    }


def _seeded_schema() -> dict[str, Any]:
    agent = next(a for a in BUILTIN_AGENTS if a["slug"] == "exposure-briefing-analyst")
    schema = _resolve_seed_schema(agent["output_schema"])
    assert schema is not None
    return schema


def test_the_schema_is_valid_and_is_the_one_the_agent_is_seeded_with() -> None:
    Draft202012Validator.check_schema(_EXPOSURE_BRIEFING_SCHEMA)
    assert _seeded_schema()["properties"]["coverage"]["properties"]["unread"]


def test_a_briefing_without_unread_still_validates() -> None:
    validate(_briefing(), _EXPOSURE_BRIEFING_SCHEMA)


def test_a_briefing_records_an_unread_simulation() -> None:
    unread = [
        {
            "product": "openaev",
            "source": "simulation Q4 external (sim-123)",
            "reason": "OpenAEV timed out searching findings scoped to simulation sim-123.",
        }
    ]
    validate(_briefing(unread=unread), _EXPOSURE_BRIEFING_SCHEMA)


@pytest.mark.parametrize(
    "entry",
    [
        {"product": "openaev", "source": "simulation sim-123"},
        {"product": "openaev", "reason": "timed out"},
        {"product": "jira", "source": "simulation sim-123", "reason": "timed out"},
    ],
)
def test_an_unread_entry_names_its_product_source_and_reason(entry: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        validate(_briefing(unread=[entry]), _EXPOSURE_BRIEFING_SCHEMA)


def test_the_persona_retries_once_then_records_the_source_as_unread() -> None:
    text = " ".join(_EXPOSURE_BRIEFING_PERSONA.split())
    assert "Retry narrower: inject_id." in text
    assert "retry ONCE with that scope" in text
    assert "`coverage.unread`" in text
    assert "never fully covered" in text
    assert '"not re-checked"' in text


def test_both_seeded_assignments_carry_the_unread_rule() -> None:
    prompts = [a["prompt"] for a in BUILTIN_ASSIGNMENTS if a["agent_slug"] == "exposure-briefing-analyst"]
    assert len(prompts) == 2
    for prompt in prompts:
        assert prompt is _EXPOSURE_BRIEFING_PROMPT
    assert "retry it ONCE with that scope" in _EXPOSURE_BRIEFING_PROMPT
    assert "`coverage.unread`" in _EXPOSURE_BRIEFING_PROMPT
    assert "end the `summary` with one clause naming what could not be read" in _EXPOSURE_BRIEFING_PROMPT
    assert "never describe it as fully covered" in _EXPOSURE_BRIEFING_PROMPT
    assert "'not re-checked'" in _EXPOSURE_BRIEFING_PROMPT


def test_the_hint_the_persona_waits_for_is_the_one_the_tool_answers() -> None:
    # The retry rule keys on the tool's own words: a timed-out simulation-scoped search answers
    # "Retry narrower: inject_id." (#2594), the exact hint the persona and the prompt name.
    from app.integrations.providers.openaev import core

    hint = core._findings_timeout_hint("Error: request timed out", "simulation sim-123", "inject_id")
    assert hint.endswith("Retry narrower: inject_id.")
    assert hint.split("Retry narrower:")[0].strip().endswith("scoped to simulation sim-123.")
    assert "Retry narrower: inject_id." in " ".join(_EXPOSURE_BRIEFING_PERSONA.split())
    assert "Retry narrower: inject_id." in _EXPOSURE_BRIEFING_PROMPT
