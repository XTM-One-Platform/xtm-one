# SPDX-License-Identifier: Apache-2.0

"""The remediation decision card reads whole (#2174, partner 0ae1fa2e).

Oversize option texts are refused, never cut; an option without figures says
why; the context names the severity and the SLA is the deadline; the blind
action never prescribes an upgrade; the needs_human event names its recipient.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import ctem_remediation_decision as rd
from app.services import ctem_version_match as matching
from app.services import work_item


@pytest.mark.parametrize(("name", "limit"), sorted(rd.TEXT_LIMITS.items()))
def test_an_option_text_over_its_room_is_refused_with_its_length(name: str, limit: int) -> None:
    option = {"key": "A", "label": "Patch", name: "w " * limit}
    with pytest.raises(rd.DecisionInputError) as refused:
        rd.parse_options([option])
    message = str(refused.value)
    assert name in message and str(limit) in message and "option A" in message


def test_an_option_text_at_its_room_is_kept_whole() -> None:
    residual = "r" * rd.TEXT_LIMITS["residual_risk"]
    (option,) = rd.parse_options([{"key": "A", "label": "l" * 120, "residual_risk": residual}])
    assert option.label == "l" * 120 and option.residual_risk == residual


def test_an_unpriced_option_says_why() -> None:
    options = rd.parse_options(
        [
            {"key": "A", "label": "Close as not relevant", "not_priced_reason": "case not relevant (40/100)"},
            {"key": "B", "label": "Upgrade"},
            {"key": "C", "label": "Priced", "cost": 100},
        ]
    )
    ask = rd.decision_ask("Chrome", options, "A", figures=True, reason="the gate waits")
    effects = [o["effect"] for o in ask["options"]]
    assert effects[0] == "not priced: case not relevant (40/100)"
    assert effects[1] == f"not priced: {rd.NOT_PRICED_DEFAULT}"
    assert "cost 100" in effects[2] and "not priced" not in effects[2]
    assert all("cost unknown" not in e for e in effects[:2])


def test_a_decider_without_figure_access_reads_no_pricing_line() -> None:
    options = rd.parse_options([{"key": "A", "label": "Upgrade", "time_to_apply": "2 days"}])
    ask = rd.decision_ask("Chrome", options, "A", figures=False, reason="the gate waits")
    assert ask["options"][0]["effect"] == "time to apply 2 days"


def test_the_context_names_the_severity_and_the_sla_is_the_deadline() -> None:
    options = rd.parse_options([{"key": "A", "label": "Upgrade"}])
    due = datetime(2026, 11, 25, 18, 35, tzinfo=UTC)
    ask = rd.decision_ask("Chrome", options, "A", figures=False, reason="the gate waits", severity="P1", sla_due_at=due)
    assert ask["context"].startswith("P1 case. The remediation decision waits")
    assert ask["deadline"] == due.isoformat()
    bare = rd.decision_ask("Chrome", options, "A", figures=False, reason="the gate waits")
    assert "deadline" not in bare and bare["context"].startswith("The remediation decision waits")


def test_the_blind_action_never_prescribes_an_upgrade_and_the_old_wording_is_not_repeated() -> None:
    assert "upgrade" not in matching.BLIND_ACTION.casefold()
    match = matching.version_match(None, product_label="Example BI", fixed_version=None)
    ask = {
        "question": "Accept as not affected?",
        "context": "Verify the running version, then upgrade: the hosts are not known to be vulnerable.\nThe case is P1.",
    }
    context = matching.complete_ask(ask, match)["context"]
    assert "then upgrade" not in context
    assert context.count(matching.BLIND_ACTION) == 1


def test_the_needs_human_event_names_its_recipient() -> None:
    person = uuid.uuid4()
    item = SimpleNamespace(
        id=uuid.uuid4(),
        needs_human=True,
        ask={"question": "Which remediation?"},
        waits_for_user_id=person,
        waits_for_group_ids=None,
        created_at=datetime.now(UTC),
    )
    name, data = work_item._needs_human_event(item, previous_status="open")
    assert name == "work_item.needs_human"
    assert data["waits_for_user_id"] == str(person) and data["waits_for_group_ids"] is None
    groups = SimpleNamespace(**{**vars(item), "waits_for_user_id": None, "waits_for_group_ids": ["g1", "g2"]})
    _, data = work_item._needs_human_event(groups, previous_status="open")
    assert data["waits_for_user_id"] is None and data["waits_for_group_ids"] == ["g1", "g2"]
