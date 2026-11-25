"""manage_opencti_security_coverage and the review gate of the CTEM loop (CTEM-16, #1089).

Pinned here without a network:
- every action sends the expected document and answers the coverage read back;
- create checks the covered object's type before writing and follows the run's draft;
- a field the action does not apply is named as ignored and the action runs;
- the capability pre-check knows the tool and its write actions;
- ``ctem.analyst_review`` maps onto the coverage request: it runs out of the box
  (the seeded level), waits at Full human gates and when pinned ``wait``.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

import app.integrations.providers.opencti.security_coverage as sc_mod
from app.agents.builtin_fleet_docs import BUILTIN_FLEET_STRUCTURED
from app.agents.fleet_seed import SEEDED_FLEET_AUTONOMY_LEVEL
from app.agents.seed import SEED_DATA_DIR
from app.integrations.providers.opencti import build_tools
from app.integrations.providers.opencti.capabilities import required_capabilities
from app.services.fleet_autonomy import effective_gates, gated_tool_rules
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.opencti.io||test-token"
TOOL = "manage_opencti_security_coverage"
_GATE = "ctem.analyst_review"
_LOOP_FLEETS = ("ctem-loop", "ctem-vuln-prioritization", "ctem-ai-security", "ctem-third-party-risk")


class _Recorder:
    def __init__(self, answers: dict[str, dict[str, Any]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any], bool]] = []

    async def __call__(
        self,
        _client: Any,
        _base_url: str,
        _headers: Any,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        live: bool = False,
    ) -> tuple[dict[str, Any], None]:
        operation = query.split("(")[0].split()[-1]
        self.calls.append((operation, variables or {}, live))
        return self.answers.get(operation, {}), None


def _build(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, dict[str, Any]] | None = None
) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(answers or {})

    @contextlib.asynccontextmanager
    async def _client(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
        yield None

    monkeypatch.setattr(sc_mod, "_client", _client)
    monkeypatch.setattr(sc_mod, "_graphql", recorder)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    return executor, recorder


async def _run(executor: CallableToolExecutor, **arguments: Any) -> str:
    result = await executor.execute_tool(ToolCall(id="call-1", name=TOOL, arguments=arguments))
    return result.content


def test_the_tool_is_offered_with_a_valid_schema() -> None:
    executor = CallableToolExecutor()
    tools = {t.name: t for t in build_tools(TOKEN, executor)}
    tool = tools[TOOL]
    assert tool.source == "integration:opencti"
    assert tool.input_schema["required"] == ["action"]
    assert tool.input_schema["properties"]["action"]["enum"] == ["list", "get", "create", "update", "delete"]
    json.dumps(tool.input_schema)


async def test_create_checks_the_covered_type_then_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec = _build(
        monkeypatch,
        {
            "CoveredType": {"stixDomainObject": {"id": "g1", "entity_type": "Grouping"}},
            "SecurityCoverageAdd": {"securityCoverageAdd": {"id": "sc1", "name": "NGINX HTTP/2"}},
        },
    )
    content = await _run(
        executor,
        action="create",
        covered_id="g1",
        name="NGINX HTTP/2",
        periodicity="P1D",
        platforms_affinity=["linux"],
    )
    assert "Created security coverage 'NGINX HTTP/2' (id: sc1)" in content
    assert [c[0] for c in rec.calls] == ["CoveredType", "SecurityCoverageAdd"]
    _, variables, live = rec.calls[1]
    assert live is False
    assert variables["input"] == {
        "name": "NGINX HTTP/2",
        "objectCovered": "g1",
        "auto_enrichment_disable": False,
        "periodicity": "P1D",
        "platforms_affinity": ["linux"],
    }


async def test_create_refuses_an_object_a_coverage_cannot_cover(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec = _build(
        monkeypatch, {"CoveredType": {"stixDomainObject": {"id": "v1", "entity_type": "Vulnerability"}}}
    )
    content = await _run(executor, action="create", covered_id="v1", name="CVE")
    assert "is a Vulnerability" in content and "Grouping" in content and "nothing was written" in content
    assert [c[0] for c in rec.calls] == ["CoveredType"]


async def test_create_needs_the_covered_object_and_a_name(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec = _build(monkeypatch)
    content = await _run(executor, action="create", covered_id="g1")
    assert "required" in content and "nothing was written" in content
    assert rec.calls == []


async def test_get_answers_scores_results_and_covered_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    node = {
        "id": "sc1",
        "name": "NGINX HTTP/2",
        "periodicity": "P1D",
        "coverage_last_result": "2026-11-22T04:00:00Z",
        "coverage_information": [{"coverage_name": "Prevention", "coverage_score": 80}],
        "objectCovered": {"id": "g1", "entity_type": "Grouping", "representative": {"main": "NGINX"}},
        "results": [{"id": "r1", "coverage_last_result": "2026-11-22T04:00:00Z", "coverage_information": []}],
        "coveredVulnerabilities": {
            "count": 1,
            "entities": [
                {
                    "coverage_information": [{"coverage_name": "Detection", "coverage_score": 40}],
                    "to": {"id": "v1", "name": "CVE-2026-123633"},
                }
            ],
        },
        "coveredAttackPatterns": {
            "count": 1,
            "entities": [{"coverage_information": [], "to": {"id": "a1", "name": "Exploit", "x_mitre_id": "T1190"}}],
        },
    }
    executor, _ = _build(monkeypatch, {"SecurityCoverage": {"securityCoverage": node}})
    content = await _run(executor, action="get", coverage_id="sc1")
    assert "Prevention 80%" in content
    assert "Grouping NGINX (id: g1)" in content
    assert "CVE-2026-123633 (id: v1): Detection 40%" in content
    assert "Exploit T1190 (id: a1): no result yet" in content
    assert "results (1)" in content


async def test_list_filters_on_the_covered_object(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec = _build(monkeypatch, {"SecurityCoverages": {"securityCoverages": {"edges": []}}})
    content = await _run(executor, action="list", covered_id="g1")
    assert content == "No security coverage found."
    filters = rec.calls[0][1]["filters"]
    assert filters["filters"][0]["key"] == ["objectCovered"] and filters["filters"][0]["values"] == ["g1"]


async def test_create_in_a_draft_says_it_waits_for_the_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder(
        {
            "CoveredType": {"stixDomainObject": {"id": "g1", "entity_type": "Grouping"}},
            "SecurityCoverageAdd": {"securityCoverageAdd": {"id": "sc1", "name": "n"}},
        }
    )

    @contextlib.asynccontextmanager
    async def _client(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
        yield None

    monkeypatch.setattr(sc_mod, "_client", _client)
    monkeypatch.setattr(sc_mod, "_graphql", recorder)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, scope_headers={"opencti-draft-id": "d1"})
    content = await _run(executor, action="create", covered_id="g1", name="n")
    assert "in OpenCTI draft d1" in content and "when the draft is validated" in content


async def test_update_and_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec = _build(monkeypatch)
    content = await _run(executor, action="update", coverage_id="sc1", periodicity="P7D")
    assert content == "Updated security coverage sc1: periodicity."
    assert rec.calls[-1][1]["input"] == [{"key": "periodicity", "value": ["P7D"]}]
    assert "Deleted security coverage sc1" in await _run(executor, action="delete", coverage_id="sc1")
    assert rec.calls[-1][0] == "SecurityCoverageDelete"


@pytest.mark.parametrize("flag", [True, False])
async def test_update_writes_the_auto_enrichment_flag_both_ways(monkeypatch: pytest.MonkeyPatch, flag: bool) -> None:
    executor, rec = _build(monkeypatch)
    content = await _run(executor, action="update", coverage_id="sc1", auto_enrichment_disable=flag)
    assert content == "Updated security coverage sc1: auto_enrichment_disable."
    assert rec.calls[-1][1]["input"] == [{"key": "auto_enrichment_disable", "value": [flag]}]


async def test_update_leaves_the_auto_enrichment_flag_when_not_given(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec = _build(monkeypatch)
    await _run(executor, action="update", coverage_id="sc1", name="Renamed")
    assert rec.calls[-1][1]["input"] == [{"key": "name", "value": ["Renamed"]}]


async def test_update_without_a_field_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, rec = _build(monkeypatch)
    content = await _run(executor, action="update", coverage_id="sc1")
    assert "nothing was written" in content and rec.calls == []


@pytest.mark.parametrize(
    ("arguments", "named"),
    [
        ({"action": "delete", "coverage_id": "sc1", "name": "x"}, "name"),
        ({"action": "get", "coverage_id": "sc1", "covered_id": "g1"}, "covered_id"),
        ({"action": "update", "coverage_id": "sc1", "covered_id": "g2"}, "covered_id"),
        ({"action": "list", "public": True}, "public"),
    ],
)
async def test_an_unapplied_field_is_named_as_ignored_and_the_action_runs(
    monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any], named: str
) -> None:
    executor, _rec = _build(monkeypatch)
    content = await _run(executor, **arguments)
    assert f"action '{arguments['action']}' does not apply" in content and named in content
    assert "ignored and the rest ran" in content


@pytest.mark.parametrize(
    ("action", "capability"),
    [
        ("list", "KNOWLEDGE"),
        ("get", "KNOWLEDGE"),
        ("create", "KNOWLEDGE_KNUPDATE"),
        ("update", "KNOWLEDGE_KNUPDATE"),
        ("delete", "KNOWLEDGE_KNUPDATE_KNDELETE"),
    ],
)
def test_the_capability_pre_check_knows_every_action(action: str, capability: str) -> None:
    assert required_capabilities(TOOL, {"action": action}) == ((capability,),)


def _review_gate(slug: str) -> dict[str, Any]:
    return next(g for g in BUILTIN_FLEET_STRUCTURED[slug]["gates"] if g["key"] == _GATE)


@pytest.mark.parametrize("slug", _LOOP_FLEETS)
def test_the_review_gate_offers_the_draft_validation_and_names_no_tool(slug: str) -> None:
    gate = _review_gate(slug)
    # The draft holding the package is offered by the gate's key (#1480, #1675), never paused.
    assert "tools" not in gate
    from app.services.fleet_autonomy import gate_tool_names

    assert "apply_opencti_draft" in gate_tool_names([gate])


@pytest.mark.parametrize("slug", _LOOP_FLEETS)
def test_the_coverage_request_runs_out_of_the_box(slug: str) -> None:
    rules = gated_tool_rules(SEEDED_FLEET_AUTONOMY_LEVEL, BUILTIN_FLEET_STRUCTURED[slug]["gates"])
    assert not any(r["key"] == _GATE for r in rules)


@pytest.mark.parametrize("persona", ["ctem-vuln-prioritization-orchestrator", "ctem-threat-response-orchestrator"])
def test_the_orchestrators_draft_the_package_and_request_its_coverage(persona: str) -> None:
    text = (SEED_DATA_DIR / "agent_persona" / f"{persona}.md").read_text()
    assert "Grouping" in text and TOOL in text
    assert "`ctem.analyst_review`" in text
    assert "analyst review skipped by policy" in text


@pytest.mark.parametrize("slug", _LOOP_FLEETS)
def test_the_coverage_request_never_pauses_even_pinned_or_at_full_gates(slug: str) -> None:
    """#1675: pinned wait, the review is a decision item; the request call itself runs."""
    gates = BUILTIN_FLEET_STRUCTURED[slug]["gates"]
    pinned = effective_gates(gates, None, pins={_GATE: "wait"})
    for rules in (gated_tool_rules(SEEDED_FLEET_AUTONOMY_LEVEL, pinned), gated_tool_rules("full_gates", gates)):
        assert not [r for r in rules if r["name"] == TOOL]
