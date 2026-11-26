# SPDX-License-Identifier: Apache-2.0

"""#2468: the validation gate states a scenario that is not playable, and the case links its scenario."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.integrations.providers.openaev._common import _aev_link, aev_ui_url
from app.services import ctem_cases as rules
from app.services import ctem_validation_scenario as scenario_mod

SCENARIO_ID = "3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
URL = f"https://aev.example.com/tenant-1/admin/scenarios/{SCENARIO_ID}"
PERSONA = (
    Path(__file__).resolve().parents[1] / "app/agents/seed_data/agent_persona/ctem-threat-response-orchestrator.md"
)


class _Db:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)


def _case() -> Any:
    return SimpleNamespace(id=uuid.uuid4(), title="APT-X intrusion set")


def _event(added: Any) -> Any:
    added.at = added.at or datetime.now(UTC)
    return added


def _record(db: _Db, case: Any, **over: Any) -> None:
    data = {
        "scenario_id": SCENARIO_ID,
        "name": "APT-X emulation",
        "url": URL,
        "techniques_total": 12,
        "techniques_playable": 1,
        "playable_tests": "a DNS beacon",
        **over,
    }
    scenario_mod.record_scenario(db, case, data, actor="agent")  # type: ignore[arg-type]


def test_tenant_scoped_link_carries_the_tenant_segment() -> None:
    assert aev_ui_url("https://aev.example.com", "scenario", SCENARIO_ID, "tenant-1") == URL
    assert aev_ui_url("https://aev.example.com/", "scenario", SCENARIO_ID) == (
        f"https://aev.example.com/admin/scenarios/{SCENARIO_ID}"
    )
    assert aev_ui_url("", "scenario", SCENARIO_ID, "tenant-1") == ""
    assert (
        _aev_link("https://aev.example.com", "scenario", SCENARIO_ID, "tenant-1") == f"  Link: [Open in OpenAEV]({URL})"
    )


def test_playability_counts() -> None:
    assert scenario_mod.playability(12, 1) == {
        "techniques_total": 12,
        "techniques_playable": 1,
        "techniques_missing": 11,
        "playable": False,
    }
    assert scenario_mod.playability(4, 4)["playable"] is True
    assert scenario_mod.playability(0, 0)["playable"] is False


def test_record_then_read_the_scenario() -> None:
    db, case = _Db(), _case()
    _record(db, case)
    [row] = [_event(r) for r in db.added]
    assert row.kind == "scenario" and row.to_value == SCENARIO_ID and row.evidence == URL
    found = scenario_mod.scenario_of([row])
    assert found is not None
    assert found["techniques_missing"] == 11 and found["playable"] is False
    assert found["playable_tests"] == "a DNS beacon"
    # The same scenario with the same figures again writes nothing.
    scenario_mod.record_scenario(
        db, case, {**found, "techniques_total": 12, "techniques_playable": 1}, found, actor="a"
    )  # type: ignore[arg-type]
    assert len(db.added) == 1


@pytest.mark.parametrize(
    "over",
    [
        {"scenario_id": "not-a-uuid"},
        {"url": "https://aev.example.com/admin/scenarios/00000000-0000-0000-0000-000000000000"},
        {"techniques_playable": 13},
        {"techniques_total": None},
    ],
)
def test_record_refuses_a_wrong_scenario(over: dict[str, Any]) -> None:
    with pytest.raises(rules.CaseRuleError):
        _record(_Db(), _case(), **over)


def test_case_page_scenario_is_restricted_without_openaev() -> None:
    db, case = _Db(), _case()
    _record(db, case)
    rows = [_event(r) for r in db.added]
    shown = scenario_mod.scenario_for(rows, SimpleNamespace(openaev=True))
    assert shown is not None and shown["url"] == URL and shown["restricted"] is False
    hidden = scenario_mod.scenario_for(rows, SimpleNamespace(openaev=False))
    assert hidden is not None and hidden["restricted"] is True and "url" not in hidden
    assert hidden["techniques_missing"] == 11
    assert scenario_mod.scenario_for([], SimpleNamespace(openaev=True)) is None


_ASK = {
    "question": "Launch the coverage scenario?",
    "context": "The scenario emulates APT-X.",
    "options": [{"key": "A", "label": "Launch", "effect": "runs it"}, {"key": "B", "label": "Ticket"}],
    "recommendation": "A",
    "links": [],
}
_PACKAGE = {
    "grouping_id": "g",
    "name": "APT-X package",
    "url": "https://cti.example.com/dashboard/analyses/groupings/x",
}


def test_not_playable_card_states_it_and_never_recommends_a_launch() -> None:
    db, case = _Db(), _case()
    _record(db, case)
    found = scenario_mod.scenario_of([_event(r) for r in db.added])
    out = scenario_mod.complete_ask(_ASK, found, _PACKAGE, "APT-X")
    assert str(out["context"]).startswith(
        "Scenario not playable: 11 of 12 techniques have no payload; the only playable injects test a DNS beacon, not APT-X."
    )
    assert [o["key"] for o in out["options"]] == ["A", "B", "C"]
    labels = " ".join(o["label"] for o in out["options"]).lower()
    assert "complete" in labels and "manually" in labels and "reject" in labels and "launch" not in labels
    assert out["recommendation"] == "A"
    urls = [link["url"] for link in out["links"]]
    assert URL in urls and _PACKAGE["url"] in urls
    # Applied twice, the card does not grow.
    assert scenario_mod.complete_ask(out, found, _PACKAGE, "APT-X")["context"] == out["context"]


def test_playable_card_is_unchanged_but_linked() -> None:
    db, case = _Db(), _case()
    _record(db, case, techniques_playable=12, playable_tests="")
    found = scenario_mod.scenario_of([_event(r) for r in db.added])
    out = scenario_mod.complete_ask(_ASK, found, None, "APT-X")
    assert out["context"] == _ASK["context"] and out["options"] == _ASK["options"]
    assert out["recommendation"] == "A"
    assert [link["url"] for link in out["links"]] == [URL]


def test_newest_scenario_wins() -> None:
    db, case = _Db(), _case()
    _record(db, case)
    _record(db, case, techniques_playable=12)
    old, new = db.added
    old.at = datetime.now(UTC) - timedelta(hours=1)
    new.at = datetime.now(UTC)
    found = scenario_mod.scenario_of([new, old])
    assert found is not None and found["playable"] is True


def test_persona_carries_the_playability_rule() -> None:
    text = PERSONA.read_text(encoding="utf-8")
    validation = text.split("3. **Validation.**", 1)[1].split("4. **Risk.**", 1)[0]
    assert "Scenario not playable: N of M techniques have no payload" in validation
    assert "OpenAEV Payload Creator" in validation
    assert "validate manually" in validation
    assert "reject and clean up" in validation
    assert "`update_ctem_case` `scenario`" in validation
