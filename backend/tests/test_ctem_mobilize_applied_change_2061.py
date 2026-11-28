# SPDX-License-Identifier: Apache-2.0

"""Mobilize applies a fix through a bound integration, not only a ticket (#2061).

The Mobilization role can be bound to the EDR/XDR and the SIEM; the Remediation
Coordinator declares each action's mode; an applied change is written on the case
with its rollback under ``ctem.production_change``, which waits or runs alone per
the fleet's level and pins like every gate.
"""

from __future__ import annotations

import json
import pathlib
import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from app.agents.builtin_fleet_docs import BUILTIN_FLEET_STRUCTURED
from app.models.ctem_case import CtemCase, CtemCaseEvent
from app.services import ctem_case_audit as audit
from app.services import ctem_case_loop as loop
from app.services import ctem_cases as rules
from app.services import fleet_autonomy
from app.services import fleet_capability_slots as slots

NOW = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)
jsonschema: Any = pytest.importorskip("jsonschema")
_SEED = pathlib.Path(__file__).resolve().parents[1] / "app/agents/seed_data"
_CHANGE = {
    "integration": "edr: Defender (lab)",
    "kind": "ioc_prevention",
    "pushed": "3 SHA-256 indicators, action Block, all devices, expiry 30 days",
    "ids": ["ind-1", "ind-2", "ind-3"],
    "rollback": "delete indicators ind-1, ind-2, ind-3",
}


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)

    def events(self, kind: str) -> list[CtemCaseEvent]:
        return [r for r in self.added if isinstance(r, CtemCaseEvent) and r.kind == kind]

    @property
    def db(self) -> Any:
        return cast(Any, self)


def _case(state: str = "analyzing") -> CtemCase:
    return CtemCase(id=uuid.uuid4(), title="Block the indicators of a relevant threat", state=state)


def test_the_mobilization_role_can_be_bound_to_the_edr_and_the_siem() -> None:
    # Search engine (#2728): vendor advisories and fixed versions for the fix it mobilises.
    assert {"ticketing", "notification", "edr_xdr", "siem", "search_engine"} == set(
        slots.slots_for_role("Mobilization")
    )


def _action(**extra: object) -> dict[str, object]:
    return {
        "title": "Block the campaign's indicators",
        "asset": "all endpoints",
        "change": "indicators in prevention mode",
        "evidence": "OpenAEV simulation 42: payload executed",
        "owner_role": "SOC",
        "ticket_draft": "",
        "retest": "re-run simulation 42: the payload is blocked",
        **extra,
    }


def test_a_coordinator_output_with_an_applied_change_validates() -> None:
    schema = json.loads((_SEED / "agent_output_schema/ctem-remediation-coordinator.json").read_text("utf-8"))
    jsonschema.validate({"actions": [_action(mode="applied_change", applied_change=_CHANGE)]}, schema)
    jsonschema.validate({"actions": [_action(mode="ticket", ticket_draft="Upgrade Chrome to 131")]}, schema)
    with pytest.raises(jsonschema.ValidationError):  # the mode is always declared
        jsonschema.validate({"actions": [_action()]}, schema)
    with pytest.raises(jsonschema.ValidationError):  # an applied change carries its rollback
        no_rollback = {k: v for k, v in _CHANGE.items() if k != "rollback"}
        jsonschema.validate({"actions": [_action(mode="applied_change", applied_change=no_rollback)]}, schema)


def test_an_applied_change_is_written_on_the_case_and_moves_it_to_remediating() -> None:
    db, case = FakeSession(), _case()
    loop.record_applied_change(db.db, case, _CHANGE, actor="agent-1", now=NOW)
    (event,) = db.events("applied_change")
    assert (event.from_value, event.to_value) == ("edr: Defender (lab)", "ioc_prevention")
    assert event.reason == _CHANGE["pushed"]
    assert json.loads(event.evidence or "") == {"ids": ["ind-1", "ind-2", "ind-3"], "rollback": _CHANGE["rollback"]}
    assert case.state == "remediating"
    assert audit.KIND_PHASE["applied_change"] == audit.KIND_PHASE["ticket"] == "remediation"


@pytest.mark.parametrize(
    "change",
    [
        {**_CHANGE, "kind": "sigma_in_prevention"},
        {**_CHANGE, "rollback": ""},
        {**_CHANGE, "integration": ""},
        {**_CHANGE, "ids": "ind-1"},
        "not an object",
    ],
)
def test_an_incomplete_applied_change_is_refused_and_nothing_is_written(change: object) -> None:
    db, case = FakeSession(), _case()
    with pytest.raises(rules.CaseRuleError):
        loop.record_applied_change(db.db, case, cast(Any, change), now=NOW)
    assert db.added == [] and case.state == "analyzing"


def test_the_production_change_gate_moves_the_case_to_remediating() -> None:
    db, case = FakeSession(), _case()
    loop.record_gate(db.db, case, key="ctem.production_change", outcome="passed_automatically", by="level low", now=NOW)
    assert case.state == "remediating"


def _production_gate() -> dict[str, object]:
    gates = BUILTIN_FLEET_STRUCTURED["ctem-vuln-prioritization"]["gates"]
    return next(g for g in gates if g["key"] == "ctem.production_change")


def test_the_production_change_gate_runs_alone_at_low_waits_at_medium_and_honours_pins() -> None:
    gate = _production_gate()
    assert "EDR" in str(gate["action"])
    assert not fleet_autonomy.gate_waits("low", gate)
    assert fleet_autonomy.gate_waits("medium", gate)
    assert fleet_autonomy.gate_waits("low", {**gate, "pin": fleet_autonomy.PIN_WAIT})
    assert not fleet_autonomy.gate_waits("medium", {**gate, "pin": fleet_autonomy.PIN_RUN})


def test_the_coordinator_persona_names_both_paths_and_never_puts_sigma_in_prevention() -> None:
    text = (_SEED / "agent_persona/ctem-remediation-coordinator.md").read_text("utf-8")
    assert "`ctem.production_change`" in text and "`applied_change`" in text and "`ctem.mobilize`" in text
    assert "Sigma stays detection" in text
    for name in ("ctem-threat-response-orchestrator.md", "ctem-vuln-prioritization-orchestrator.md"):
        orchestrator = (_SEED / "agent_persona" / name).read_text("utf-8")
        assert "Applying a change on production through an integration is the gate `ctem.production_change`" in (
            orchestrator
        ), name
