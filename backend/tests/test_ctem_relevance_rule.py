# SPDX-License-Identifier: Apache-2.0

"""One relevance rule for the threat response seed and its PIR floor (#1678).

The seeded "Threat signal recorded" run starts only on a relevant threat signal
an organization PIR flags at or above the policy's ``pir_min_score_threat``
(event field ``pir_floor_met``); the analyst persona, the orchestrator persona
and the prompts read the same rule.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents import builtin_agents as ba
from app.services import ctem_signals as signals
from app.services import platform_events as pe
from app.services.ctem_trigger_rules import PIR_FLOOR_KEY, pir_floor_met
from app.services.integration_event_filters import payload_filter_reason

_PERSONAS = Path(ba.__file__).parent / "seed_data" / "agent_persona"
_RULE_CORE = (
    "A threat is relevant when its relevance score reaches the prioritization policy's relevance_min_score_threat "
    "(read live in the organization context, never a remembered value), or when an organization PIR flags it "
    "with a PIR score at or above the prioritization policy's pir_min_score_threat; a PIR flag below that "
    "threshold is one factor of the score, never relevance by itself."
)


def _assignment(name: str) -> dict:
    return next(a for a in ba.CTEM_F1_ASSIGNMENTS if a["name"] == name)


def _signal(evidence: dict | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        state="to_validate",
        trigger_path="relevant_threat",
        lens="world",
        source_provider="opencti",
        subject_kind="threat",
        case_id=None,
        evidence=evidence,
    )


def test_pir_floor_met_needs_one_pir_score_at_or_above_the_threshold():
    assert pir_floor_met([{"pir_id": "a", "score_at_delivery": 75}], 75)
    assert pir_floor_met([{"pir_id": "a", "score_at_delivery": 10}, {"pir_id": "b", "score_at_delivery": 90}], 75)
    assert not pir_floor_met([{"pir_id": "a", "score_at_delivery": 74}], 75)
    assert not pir_floor_met([], 75)
    assert not pir_floor_met(None, 75)
    assert not pir_floor_met([{"pir_id": "a", "score_at_delivery": True}], 0)
    assert not pir_floor_met([{"pir_id": "a"}], 0)


def test_the_signal_event_carries_the_floor_and_no_evidence_value():
    assert "pir_floor_met" in pe.PLATFORM_EVENTS[signals.SIGNAL_RECORDED]["filter_keys"]
    assert "pir_floor_met" in pe.PLATFORM_EVENTS[signals.SIGNAL_TRIAGED]["filter_keys"]
    met = signals.signal_event_data(_signal({PIR_FLOOR_KEY: True, "pir": [{"pir_id": "a"}]}))
    assert met["pir_floor_met"] is True and "evidence" not in met and "pir" not in met
    assert signals.signal_event_data(_signal({PIR_FLOOR_KEY: False}))["pir_floor_met"] is False
    assert signals.signal_event_data(_signal(None))["pir_floor_met"] is False
    assert signals.signal_event_data(_signal({PIR_FLOOR_KEY: "yes"}))["pir_floor_met"] is False


def test_the_threat_signal_run_filters_on_the_floor():
    flt = _assignment(ba.CTEM_F1_THREAT_SIGNAL)["trigger_config"]["filter"]
    assert flt["pir_floor_met"] is True
    base = signals.signal_event_data(_signal({PIR_FLOOR_KEY: True}))
    assert payload_filter_reason(base, flt) is None
    below = signals.signal_event_data(_signal({PIR_FLOOR_KEY: False}))
    assert payload_filter_reason(below, flt) is not None
    # An event built before the field existed holds no floor: dropped, never widened.
    legacy = {k: v for k, v in base.items() if k != "pir_floor_met"}
    assert payload_filter_reason(legacy, flt) is not None


@pytest.mark.asyncio
async def test_the_listener_floor_reads_the_policy_threshold(monkeypatch):
    from app.services import ctem_signal_listeners as listeners
    from app.services import organization_context as org_context

    policy = org_context.PrioritizationPolicy(pir_min_score_threat=60)

    async def profile(db, organization_id):
        return object(), SimpleNamespace(prioritization_policy=policy)

    monkeypatch.setattr(org_context, "current_profile", profile)
    assert await listeners._pir_floor(None, uuid.uuid4(), [{"pir_id": "a", "score_at_delivery": 60}])
    assert not await listeners._pir_floor(None, uuid.uuid4(), [{"pir_id": "a", "score_at_delivery": 59}])
    assert not await listeners._pir_floor(None, uuid.uuid4(), [])

    async def broken(db, organization_id):
        raise RuntimeError("no profile")

    # A profile that cannot be read holds the policy default (75).
    monkeypatch.setattr(org_context, "current_profile", broken)
    assert not await listeners._pir_floor(None, uuid.uuid4(), [{"pir_id": "a", "score_at_delivery": 70}])
    assert await listeners._pir_floor(None, uuid.uuid4(), [{"pir_id": "a", "score_at_delivery": 75}])


def test_one_rule_in_the_seed_the_personas_and_the_prompts():
    assert ba.CTEM_RELEVANCE_RULE == _RULE_CORE
    assert ba.CTEM_RELEVANCE_MIN_SCORE == 50
    for name in (ba.CTEM_F1_THREAT_SIGNAL, "Weekly threat sweep"):
        assert ba.CTEM_RELEVANCE_RULE in _assignment(name)["prompt"], name
    for slug in ("ctem-threat-relevance-analyst", "ctem-threat-response-orchestrator"):
        text = (_PERSONAS / f"{slug}.md").read_text()
        assert _RULE_CORE in text, slug
    analyst = (_PERSONAS / "ctem-threat-relevance-analyst.md").read_text()
    # The former second definition (any PIR flag is relevance) is gone.
    assert "or when a PIR flags the threat." not in analyst


def test_no_persona_or_prompt_hard_codes_the_relevance_bar() -> None:
    """#2798: the bar is the policy's relevance_min_score_threat, read live, never a number in the text."""
    bar = re.compile(r"(?i)reaches \d+ of 100|relevant from \d+|score (?:of|at) \d+ or (?:more|above)")
    for slug in ("ctem-threat-relevance-analyst", "ctem-threat-response-orchestrator"):
        assert not bar.search((_PERSONAS / f"{slug}.md").read_text()), slug
    for name in (ba.CTEM_F1_THREAT_SIGNAL, "Weekly threat sweep"):
        assert not bar.search(_assignment(name)["prompt"]), name


def test_the_policy_relevance_bar_defaults_to_50_and_is_bounded() -> None:
    from pydantic import ValidationError

    from app.services import organization_context as org_context

    assert org_context.PrioritizationPolicy().relevance_min_score_threat == 50 == ba.CTEM_RELEVANCE_MIN_SCORE
    assert org_context.PrioritizationPolicy(relevance_min_score_threat=40).relevance_min_score_threat == 40
    for bad in (-1, 101):
        with pytest.raises(ValidationError):
            org_context.PrioritizationPolicy(relevance_min_score_threat=bad)
    assert "prioritization_policy.relevance_min_score_threat" in org_context.POLICY_SETTABLE_PATHS


def test_a_lower_policy_bar_changes_the_outcome_without_a_persona_edit() -> None:
    from app.services.builtin_tools.ctem_relevance import correct_relevance_answer

    # Weighted total 49: not relevant at the default bar, relevant at a policy bar of 40.
    factors = {"sector": 40, "region": 40, "software": 50, "primary_assets": 60, "pir": 40, "activity": 60}
    assert json.loads(correct_relevance_answer(_answer(49, False, factors)))["threats"][0]["relevant"] is False
    out = json.loads(correct_relevance_answer(_answer(49, False, factors), 40))
    assert out["threats"][0]["relevant"] is True


@pytest.mark.asyncio
async def test_the_live_bar_reads_the_policy_and_falls_back_to_the_default(monkeypatch) -> None:
    from contextlib import asynccontextmanager

    from app.services import organization_context as org_context
    from app.services.builtin_tools import ctem_relevance

    @asynccontextmanager
    async def _session():
        yield None

    monkeypatch.setattr("app.core.database.async_session", _session)

    async def profile(db, organization_id=None):
        policy = org_context.PrioritizationPolicy(relevance_min_score_threat=40)
        return object(), SimpleNamespace(prioritization_policy=policy)

    monkeypatch.setattr(org_context, "current_profile", profile)
    assert await ctem_relevance.live_relevance_bar() == 40

    async def broken(db, organization_id=None):
        raise RuntimeError("no profile")

    monkeypatch.setattr(org_context, "current_profile", broken)
    assert await ctem_relevance.live_relevance_bar() == 50


# Fixed factor set and weights of the analyst (#2431), one constant for the persona and the
# finalize step that computes the score (#2763).
_FACTORS = {name: weight / 100 for name, weight in ba.CTEM_RELEVANCE_WEIGHTS.items()}


def _analyst() -> str:
    return (_PERSONAS / "ctem-threat-relevance-analyst.md").read_text()


def test_the_analyst_names_every_factor_with_a_fixed_weight_summing_to_one():
    analyst = _analyst()
    found = {m.group(1): float(m.group(2)) for m in re.finditer(r"`(\w+)` weight (0\.\d+)", analyst)}
    assert found == _FACTORS
    assert round(sum(found.values()), 6) == 1.0
    assert "the weighted sum of the factor scores you wrote" in analyst
    assert "Never another factor, never another weight." in analyst
    assert sum(ba.CTEM_RELEVANCE_WEIGHTS.values()) == 100


def test_the_analyst_never_adds_up_the_score_itself() -> None:
    analyst = _analyst()
    assert "write the sum so it can be checked" not in analyst
    assert "the platform computes it from your factors" in analyst
    assert "never add them up in `reason`" in analyst


def test_the_analyst_finalizes_on_the_computed_score() -> None:
    analyst = next(a for a in ba.CTEM_FLEET_AGENTS if a["slug"] == "ctem-threat-relevance-analyst")
    assert analyst["finalize_tool_ref"] == {"source": "builtin", "tool_name": "ctem_relevance_score"}


def _answer(score: int, relevant: bool, factors: dict[str, int]) -> str:
    threat = {
        "stix_id": "intrusion-set--1",
        "name": "T",
        "relevant": relevant,
        "score": score,
        "factors": [{"name": n, "score": s, "evidence": "e"} for n, s in factors.items()],
        "reason": "r",
    }
    return json.dumps({"threats": [threat]})


_OBSERVED = {"sector": 40, "region": 50, "software": 60, "primary_assets": 70, "pir": 45, "activity": 80}


def test_the_weighted_score_of_the_observed_factors_is_58() -> None:
    from app.services.builtin_tools.ctem_relevance import weighted_score

    factors = [{"name": n, "score": s} for n, s in _OBSERVED.items()]
    assert weighted_score(factors) == 58
    # Half up: 22.5 rounds to 23, where Python's round() would give the even 22.
    assert (
        weighted_score(
            [{"name": "pir", "score": 50}, {"name": "sector", "score": 50}, {"name": "software", "score": 25}]
        )
        == 23
    )
    assert weighted_score([{"name": "pir", "score": 1}]) == 0
    for bad in (
        None,
        [{"name": "other", "score": 1}],
        [{"name": "pir", "score": 101}],
        [{"name": "pir", "score": True}],
        [{"name": "pir", "score": 1}, {"name": "pir", "score": 2}],
    ):
        assert weighted_score(bad) is None, bad


def test_a_wrong_submitted_score_is_corrected(caplog: pytest.LogCaptureFixture) -> None:
    from app.services.builtin_tools.ctem_relevance import correct_relevance_answer

    out = json.loads(correct_relevance_answer(_answer(56, True, _OBSERVED)))
    assert out["threats"][0]["score"] == 58 and out["threats"][0]["relevant"] is True
    assert "56 -> 58" in caplog.text


def test_a_right_answer_is_returned_untouched() -> None:
    from app.services.builtin_tools.ctem_relevance import correct_relevance_answer

    content = _answer(58, True, _OBSERVED)
    assert correct_relevance_answer(content) is content
    for other in ("not json", json.dumps({"verdict": "x"}), json.dumps({"threats": [{"name": "no factors"}]})):
        assert correct_relevance_answer(other) is other


def test_a_score_over_the_bar_by_a_slip_ends_not_relevant() -> None:
    from app.services.builtin_tools.ctem_relevance import correct_relevance_answer

    # 0.15*40 + 0.10*40 + 0.30*50 + 0.20*60 + 0.15*40 + 0.10*60 = 49
    factors = {"sector": 40, "region": 40, "software": 50, "primary_assets": 60, "pir": 40, "activity": 60}
    out = json.loads(correct_relevance_answer(_answer(51, True, factors)))
    assert out["threats"][0]["score"] == 49 and out["threats"][0]["relevant"] is False


def test_a_threat_relevant_by_the_pir_floor_stays_relevant() -> None:
    from app.services.builtin_tools.ctem_relevance import correct_relevance_answer

    # The analyst called it relevant under the bar: the PIR floor rule did, and it holds.
    factors = {"sector": 0, "region": 0, "software": 0, "primary_assets": 0, "pir": 90, "activity": 50}
    out = json.loads(correct_relevance_answer(_answer(20, True, factors)))
    assert out["threats"][0]["score"] == 19 and out["threats"][0]["relevant"] is True


@pytest.mark.asyncio
async def test_the_finalize_tool_answers_the_corrected_json(monkeypatch) -> None:
    from app.services.builtin_tools import ctem_relevance
    from app.services.builtin_tools.ctem_relevance import TOOL_NAME, build_ctem_relevance_score_tool
    from app.tools.base import ToolCall

    async def _bar() -> int:
        return 50

    monkeypatch.setattr(ctem_relevance, "live_relevance_bar", _bar)

    defs, executor = build_ctem_relevance_score_tool()
    assert [d.name for d in defs] == [TOOL_NAME]
    assert next(iter(defs[0].input_schema["properties"])) == "response"
    result = await executor.execute_tool(
        ToolCall(id="c", name=TOOL_NAME, arguments={"response": _answer(56, True, _OBSERVED)})
    )
    assert not result.is_error
    assert json.loads(result.content)["threats"][0]["score"] == 58


def test_the_analyst_caps_an_unconfirmed_software_exposure():
    analyst = _analyst()
    assert "cap the `software` factor at 70" in analyst
    assert '"exposure unconfirmed"' in analyst
    for unconfirmed in ("SDK", "library", "package", "no CPE", "no version"):
        assert unconfirmed in analyst, unconfirmed
    assert "a CPE match" in analyst
    assert "`exposure_confirmed`" in analyst.split("## Output", 1)[1]


def test_the_analyst_schema_carries_the_factor_set_and_the_exposure_flag():
    schema = json.loads((_PERSONAS.parent / "agent_output_schema" / "ctem-threat-relevance-analyst.json").read_text())
    entry = schema["properties"]["threats"]["items"]
    assert entry["properties"]["exposure_confirmed"]["type"] == "boolean"
    assert "exposure_confirmed" in entry["required"]
    assert set(entry["properties"]["factors"]["items"]["properties"]["name"]["enum"]) == set(_FACTORS)


def test_the_loop_routes_a_relevant_unconfirmed_threat_to_a_version_check():
    loop = (_PERSONAS / "ctem-threat-response-orchestrator.md").read_text()
    relevance = loop.split("1. **Relevance.**", 1)[1].split("2. **Review", 1)[0]
    assert "`exposure_confirmed` false" in relevance
    assert "identify the running version" in relevance
    assert "deferral" in relevance
