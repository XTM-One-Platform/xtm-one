"""The ``cti.container_report`` intent is served out of the box.

OpenCTI's "Ask AI" container report generation routes through the intent
catalog when XTM One is configured. The catalog lists only intents with a
bound agent and has no fallback, so without a built-in binding a default
install would answer "no agent" and the feature would stay on the legacy
in-platform AI - the failure the issue reports.

The binding lands on a SHARED built-in agent: OpenCTI's AI Insights already
calls OpenCTI Entity Insights for its container summary, containers digest,
activity, forecast and history modes, and the platform chat call carries no
intent - the agent picks its mode from the request text alone. So the report
contract is pinned together with what every existing caller relies on.
"""

from __future__ import annotations

import re

from app.agents.builtin_agents import BUILTIN_AGENTS
from app.agents.seed import _INTENT_DESCRIPTIONS, BUILTIN_INTENT_BINDINGS, _resolve_seed_content

_SLUG = "opencti-entity-insights"
_INTENT = "cti.container_report"


def _agent() -> dict:
    return next(a for a in BUILTIN_AGENTS if a.get("slug") == _SLUG)


def _persona() -> str:
    persona = _resolve_seed_content("agent_persona", _agent().get("persona", _SLUG))
    assert persona, f"{_SLUG} persona must resolve to a seed file under seed_data/agent_persona/"
    return persona


def _section(marker: str) -> str:
    """The persona section whose ``## `` heading contains *marker*."""
    sections = re.split(r"^## ", _persona(), flags=re.MULTILINE)[1:]
    found = [section for section in sections if marker in section.splitlines()[0]]
    assert len(found) == 1, f"expected one persona section headed {marker!r}, found {len(found)}"
    return found[0]


def test_container_report_intent_is_bound_to_entity_insights_on_cti():
    rows = [row for row in BUILTIN_INTENT_BINDINGS if row["intent_name"] == _INTENT]

    assert rows, f"{_INTENT} must appear in BUILTIN_INTENT_BINDINGS"
    assert [(row["agent_slug"], row["vertical"]) for row in rows] == [(_SLUG, "cti")]


def test_container_report_intent_has_a_description():
    assert _INTENT_DESCRIPTIONS.get(_INTENT)
    row = next(row for row in BUILTIN_INTENT_BINDINGS if row["intent_name"] == _INTENT)
    assert row["intent_description"] == _INTENT_DESCRIPTIONS[_INTENT]


def test_entity_insights_answers_in_html():
    # The platform chat call has no per-call text format, so the agent's own
    # output format is what OpenCTI receives: its report dialog offers only
    # HTML on this path, and AI Insights renders HTML too.
    assert _agent()["output_format"] == "html"
    # The agent can read a container: it carries the OpenCTI integration the
    # report section's steps call.
    assert "opencti" in _agent()["integration_template_bindings"]


def test_every_intent_the_agent_serves_has_its_own_mode():
    persona = _persona()
    intents = _agent()["supported_intents"]

    for intent in intents:
        assert persona.count(f"(intent: {intent})") == 1, f"{intent} needs exactly one persona section"
    assert f"You handle {len(intents)} types of analysis" in persona


def test_container_report_section_pins_the_report_contract():
    section = _section(f"(intent: {_INTENT})").lower()

    # A titled report in paragraphs, organised like the legacy OpenCTI prompt.
    for phrase in ("title", "no bullet points", "victims", "intrusion", "execution", "persistence", "infrastructure"):
        assert phrase in section, phrase
    # The dialog's parameters, with the legacy ceiling on the paragraph count.
    for phrase in ("paragraphs (at most 20)", "tone", "language"):
        assert phrase in section, phrase
    # The indicators close the report, as an HTML table.
    assert "<table>" in section
    assert "indicator of compromise" in section
    # OpenCTI keeps the answer as is: nothing around the report.
    assert "report only" in section
    assert "no code fences" in section
    # The format is the agent's own (HTML): the report asks for no other.
    assert "markdown" not in section


def test_existing_modes_keep_the_contract_their_callers_parse():
    rules = _section("Output Rules (all modes)")
    assert "HTML format only" in rules
    assert "Never use Markdown syntax" in rules
    assert "<html>" in rules and "wrappers" in rules
    # AI Insights reads the first tag of these two modes.
    assert '<meta name="topics"' in _section("(intent: cti.containers_digest)")
    assert '<meta name="trend"' in _section("(intent: cti.entity_activity)")
    # The report's table is its own: no other mode is asked for one.
    assert "<table>" not in _persona().replace(_section(f"(intent: {_INTENT})"), "")
