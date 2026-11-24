# SPDX-License-Identifier: Apache-2.0

"""One relevance rule for the threat response seed and its PIR floor (#1678).

The seeded "Threat signal recorded" run starts only on a relevant threat signal
an organization PIR flags at or above the policy's ``pir_min_score_threat``
(event field ``pir_floor_met``); the analyst persona, the orchestrator persona
and the prompts read the same rule.
"""

from __future__ import annotations

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
    "A threat is relevant when its relevance score reaches 50 of 100, or when an organization PIR flags it "
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
