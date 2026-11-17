"""A role label edit never lapses a former dismissal; a regeneration drops a fixed lapsed point (#12181).

Pinned here, without a database:

1. A dismissal of the former shape (the member's whole print, written before #12099) is
   moved to its facet print while it holds (``settled_dismissals``); after that, a role
   label, parent or exposure edit lapses none, a description edit still lapses a
   description dismissal. A lapsed former record is left as is.
2. ``settle_dismissals`` (run by ``update_member`` and ``set_exposed_agents`` before the
   edit) rewrites the stored records once and reports no change on a settled fleet.
3. A whole regeneration drops a dismissal lapsed by a change of its member whose point it
   no longer raises (``without_unraised_lapsed``): the fixed point is not given back as a
   lapsed suggestion. A point raised again, a dismissal lapsed by age and a good-practice
   record are kept.
4. ``documentation_text`` reads the stored sections, narrowed to the lines naming a member.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import fleet_docs
from app.services.fleet_doc_findings import (
    DISMISSAL_DAYS,
    clean_findings,
    dismissal_record,
    lapse_reason,
    settled_dismissals,
    with_lapsed,
    without_unraised_lapsed,
)

DESC = {
    "kind": "description_mismatch",
    "member": "Analyst",
    "subject": "description",
    "finding": "Analyst's description says it orchestrates six desks; the fleet has seven.",
}
GATE = {
    "kind": "no_gate_on_sensitive_action",
    "member": "Analyst",
    "subject": "create_github_pr",
    "finding": "Analyst opens pull requests with no approval gate.",
}


def _agent(**over) -> SimpleNamespace:
    base = {
        "id": uuid.UUID(int=1),
        "name": "Analyst",
        "slug": "analyst",
        "description": "Orchestrates six desks.",
        "persona": "You triage.",
        "builtin_tool_ids": ["web"],
        "integration_bindings": [],
        "mcp_server_ids": [],
        "skill_ids": [],
        "handover_agent_ids": [],
        "handover_types": {},
        "human_in_the_loop": False,
        "approve_tool_calls": None,
    }
    return SimpleNamespace(**{**base, **over})


def _row(**over) -> SimpleNamespace:
    base = {
        "id": uuid.UUID(int=7),
        "agent_id": uuid.UUID(int=1),
        "role": "member",
        "role_label": "Analyst",
        "parent_member_id": None,
        "is_exposed": False,
        "context_inclusion": None,
        "collaboration_instruction": None,
        "organization_scope": None,
    }
    return SimpleNamespace(**{**base, **over})


def _fleet_row() -> SimpleNamespace:
    return SimpleNamespace(
        name="Office",
        description="",
        skills=[],
        default_context_inclusion="none",
        default_collaboration_instruction="",
        orchestrator_agent_id=None,
    )


def _prints(agent=None, row=None) -> dict[str, str]:
    agent = agent or _agent()
    return fleet_docs.member_fingerprints(_fleet_row(), [row or _row()], {agent.id: agent}, [])


def _former(finding: dict, prints: dict[str, str], **kw) -> dict:
    """A record as #12099's predecessors wrote it: the member's whole print, no facet."""
    [clean] = clean_findings([finding])
    [record] = dismissal_record({}, clean, prints["analyst"], by="u", **kw)
    assert "print_facet" not in record
    return record


# ── 1. a former record is settled on its facet ─────────────────────────────


@pytest.mark.parametrize(
    "edited",
    [
        {"role_label": "Senior analyst"},
        {"parent_member_id": uuid.UUID(int=9)},
        {"is_exposed": True},
    ],
)
def test_a_settled_former_record_survives_a_member_edit(edited) -> None:
    before = _prints()
    after = _prints(row=_row(**edited))
    for finding in (DESC, GATE):
        record = _former(finding, before)
        [settled], changed = settled_dismissals([record], before)
        assert changed and settled["print_facet"], finding["kind"]
        assert lapse_reason(settled, after) is None, (finding["kind"], edited)


def test_a_settled_description_record_still_lapses_on_a_description_edit() -> None:
    before = _prints()
    [settled], _ = settled_dismissals([_former(DESC, before)], before)
    after = _prints(agent=_agent(description="Orchestrates seven desks."))
    assert lapse_reason(settled, after) == "Analyst was reconfigured since (description)"


def test_a_lapsed_former_record_is_left_as_is() -> None:
    before = _prints()
    record = _former(DESC, before)
    after = _prints(row=_row(role_label="Other"))
    out, changed = settled_dismissals([record], after)
    assert not changed and out == [record]


def test_a_record_already_on_its_facet_is_not_rewritten() -> None:
    prints = _prints()
    [settled], _ = settled_dismissals([_former(DESC, prints)], prints)
    out, changed = settled_dismissals([settled], prints)
    assert not changed and out == [settled]


# ── 2. settle_dismissals before a member edit ──────────────────────────────


@pytest.mark.asyncio
async def test_settle_dismissals_rewrites_the_stored_records_once(monkeypatch) -> None:
    prints = _prints()

    async def _member_prints(_db, _fleet):
        return dict(prints)

    monkeypatch.setattr(fleet_docs, "current_member_fingerprints", _member_prints)
    fleet = SimpleNamespace(documentation={"sections": {"purpose": "x"}, "dismissed_findings": [_former(DESC, prints)]})
    assert await fleet_docs.settle_dismissals(None, fleet) is True
    [record] = fleet.documentation["dismissed_findings"]
    assert record["print_facet"]
    assert lapse_reason(record, _prints(row=_row(role_label="Retest"))) is None
    assert await fleet_docs.settle_dismissals(None, fleet) is False


@pytest.mark.asyncio
async def test_settle_dismissals_is_a_no_op_without_documentation() -> None:
    assert await fleet_docs.settle_dismissals(None, SimpleNamespace(documentation=None)) is False


# ── 3. a regeneration drops a fixed lapsed point ───────────────────────────


def test_a_regeneration_that_no_longer_raises_a_lapsed_point_drops_it() -> None:
    before = _prints()
    [settled], _ = settled_dismissals([_former(DESC, before)], before)
    fixed = _prints(agent=_agent(description="Orchestrates seven desks."))
    assert lapse_reason(settled, fixed)
    kept = without_unraised_lapsed([settled], fixed, [])
    assert kept == []
    assert with_lapsed({"dismissed_findings": kept}, fixed, []) == []


def test_a_point_raised_again_keeps_its_lapsed_dismissal() -> None:
    before = _prints()
    [settled], _ = settled_dismissals([_former(DESC, before)], before)
    changed = _prints(agent=_agent(description="Orchestrates six desks, and reads mail."))
    [again] = clean_findings([DESC])
    assert without_unraised_lapsed([settled], changed, [again]) == [settled]


def test_a_holding_dismissal_and_one_lapsed_by_age_are_kept() -> None:
    prints = _prints()
    holding = _former(GATE, prints)
    old = _former(DESC, prints, now=datetime.now(UTC) - timedelta(days=DISMISSAL_DAYS + 1))
    assert without_unraised_lapsed([holding, old], prints, []) == [holding, old]


def test_a_good_practice_record_is_kept() -> None:
    prints = _prints()
    # A practice suggestion is computed live, never cleaned as a generated finding: its record as stored.
    record = {"id": "c0ffee000001", "kind": "member_chat_open", "member": "Analyst", "member_print": "stale"}
    assert without_unraised_lapsed([record], prints, []) == [record]


# ── 4. the documentation text, by member ───────────────────────────────────


def test_documentation_text_narrows_to_a_member() -> None:
    fleet = SimpleNamespace(
        documentation={
            "sections": {
                "purpose": "The office.\nThe Analyst writes numbers.",
                "flow": "The Assistant routes work.",
            }
        }
    )
    assert fleet_docs.documentation_text(fleet) == [
        ("purpose", "The office.\nThe Analyst writes numbers."),
        ("flow", "The Assistant routes work."),
    ]
    assert fleet_docs.documentation_text(fleet, "analyst") == [("purpose", "The Analyst writes numbers.")]
    assert fleet_docs.documentation_text(SimpleNamespace(documentation=None), "x") == []
