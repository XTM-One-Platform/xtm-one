"""ROI declarations: resolution order and counting modes (issue #9285, pure).

``dev-docs/roi-outcomes.md`` section 3.2: assignment, then agent, then the
agent's legacy ``x-xtm-roi``, then the home fleet; ``per_run`` counts any
completed run, ``per_output_item`` reads the structured output as before,
``per_work_item`` never counts at the run.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.services import roi_records
from app.services.roi_records import (
    COUNT_PER_OUTPUT_ITEM,
    COUNT_PER_RUN,
    COUNT_PER_WORK_ITEM,
    ResolvedDeclaration,
    pick_declaration,
    units_of_run,
    units_of_work_item,
    validate_declaration,
)

_LEGACY = {"type": "object", "x-xtm-roi": {"unit": "cve_assessed", "count": "cves"}}


def _row(unit: str, mode: str = COUNT_PER_RUN, path: str | None = None, per: int = 1):
    return SimpleNamespace(unit=unit, count_mode=mode, count_path=path, units_per_count=per)


def test_the_assignment_declaration_wins_over_everything():
    got = pick_declaration(
        assignment=_row("contract_reviewed"),
        agent=_row("email_triaged"),
        output_schema=_LEGACY,
        fleet=_row("meeting_brief"),
    )
    assert got is not None and got.unit == "contract_reviewed" and got.source == roi_records.SOURCE_ASSIGNMENT


def test_the_agent_declaration_wins_over_the_legacy_schema_and_the_fleet():
    got = pick_declaration(assignment=None, agent=_row("email_triaged"), output_schema=_LEGACY, fleet=_row("x"))
    assert got is not None and got.unit == "email_triaged" and got.source == roi_records.SOURCE_AGENT


def test_the_legacy_schema_is_unchanged_when_nothing_else_declares():
    got = pick_declaration(assignment=None, agent=None, output_schema=_LEGACY, fleet=_row("meeting_brief"))
    assert got == ResolvedDeclaration(
        unit="cve_assessed",
        count_mode=COUNT_PER_OUTPUT_ITEM,
        count_path="cves",
        units_per_count=1,
        source=roi_records.SOURCE_OUTPUT_SCHEMA,
    )


def test_the_fleet_default_is_inherited_last():
    got = pick_declaration(assignment=None, agent=None, output_schema=None, fleet=_row("meeting_brief"))
    assert got is not None and got.unit == "meeting_brief" and got.source == roi_records.SOURCE_FLEET


def test_nothing_declared_is_none():
    assert pick_declaration(assignment=None, agent=None, output_schema={"type": "object"}, fleet=None) is None


def test_per_run_counts_a_completed_text_run_and_nothing_else():
    d = ResolvedDeclaration(unit="email_triaged", count_mode=COUNT_PER_RUN, units_per_count=3)
    assert units_of_run(d, status="completed", result_summary="Plain prose, no JSON.") == 3
    assert units_of_run(d, status="failed", result_summary=None) == 0


def test_per_output_item_matches_the_legacy_count():
    d = ResolvedDeclaration(unit="cve_assessed", count_mode=COUNT_PER_OUTPUT_ITEM, count_path="cves")
    out = json.dumps({"cves": [1, 2, 3]})
    assert units_of_run(d, status="completed", result_summary=out) == 3
    assert units_of_run(d, status="completed", result_summary="not json") == 0


def test_per_work_item_never_counts_at_the_run():
    d = ResolvedDeclaration(unit="ticket_resolved", count_mode=COUNT_PER_WORK_ITEM)
    assert units_of_run(d, status="completed", result_summary="done") is None


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"subject_type": "team"}, "subject_type"),
        ({"count_mode": "per_day"}, "count_mode"),
        ({"unit": "unknown_unit"}, "not a known unit"),
        ({"count_mode": COUNT_PER_OUTPUT_ITEM, "count_path": " "}, "count_path"),
        ({"units_per_count": 0}, "units_per_count"),
        ({"count_mode": COUNT_PER_WORK_ITEM, "count_path": "prs"}, "selector"),
        ({"count_mode": COUNT_PER_WORK_ITEM, "count_path": "result_contains: | "}, "at least one text"),
    ],
)
def test_a_bad_declaration_names_its_field(kwargs, message):
    base = {
        "subject_type": "agent",
        "unit": "email_triaged",
        "count_mode": COUNT_PER_RUN,
        "count_path": None,
        "units_per_count": 1,
        "known": {"email_triaged"},
    }
    with pytest.raises(ValueError, match=message):
        validate_declaration(**{**base, **kwargs})


def test_a_known_declaration_passes():
    validate_declaration(
        subject_type="fleet",
        unit="email_triaged",
        count_mode=COUNT_PER_WORK_ITEM,
        count_path=None,
        units_per_count=2,
        known={"email_triaged"},
    )


def _per_item(path: str | None, per: int = 1) -> ResolvedDeclaration:
    return ResolvedDeclaration(
        unit="change_delivered_pr_merged", count_mode=COUNT_PER_WORK_ITEM, count_path=path, units_per_count=per
    )


def test_per_work_item_without_selector_counts_every_done_item():
    """#10412: no selector keeps the #9285 behaviour."""
    assert units_of_work_item(_per_item(None, per=2), payload=None, result=None) == 2


def test_pull_request_merged_counts_only_an_item_naming_a_merged_pull_request():
    d = _per_item(roi_records.SELECT_PULL_REQUEST_MERGED)
    merged = {
        "pull_requests": [
            {"repo": "o/r", "number": 1, "state": "closed"},
            {"repo": "o/r", "number": 2, "state": "merged"},
        ]
    }
    open_only = {"pull_requests": [{"repo": "o/r", "number": 1, "state": "open"}]}
    assert units_of_work_item(d, payload=merged, result="") == 1
    assert units_of_work_item(d, payload=open_only, result="merged") == 0
    # A flaky-test or comment item names no pull request: not a merged change.
    assert units_of_work_item(d, payload={}, result="merged as abc") == 0
    assert units_of_work_item(d, payload=None, result=None) == 0
    assert units_of_work_item(d, payload={"pull_requests": "bad"}, result=None) == 0


def test_result_contains_counts_only_a_matching_result_case_insensitively():
    d = ResolvedDeclaration(
        unit="ui_review", count_mode=COUNT_PER_WORK_ITEM, count_path="result_contains:APPROVE|CHANGES REQUESTED"
    )
    assert units_of_work_item(d, payload=None, result="UX Director, round 3: approve") == 1
    assert units_of_work_item(d, payload=None, result="Verdict: CHANGES REQUESTED (2 blocking)") == 1
    assert units_of_work_item(d, payload=None, result="Gate closed, nothing to review") == 0
    assert units_of_work_item(d, payload=None, result=None) == 0


def test_a_work_item_selector_is_validated_and_valid_ones_pass():
    for path in (None, "", roi_records.SELECT_PULL_REQUEST_MERGED, "result_contains:APPROVE"):
        assert roi_records.work_item_selector_error(path) is None
    assert roi_records.work_item_selector_error("payload.prs") is not None


async def test_record_run_units_without_declaration_keeps_the_legacy_path():
    """No resolved declaration and no ``x-xtm-roi``: nothing is written, no query."""

    class _NoDb:
        def __getattr__(self, name):  # any DB access fails the test
            raise AssertionError(f"unexpected db.{name}")

    run = SimpleNamespace(id=None, agent_id=None, status="completed", result_summary="text")
    assert await roi_records.record_run_units(_NoDb(), run=run, output_schema={"type": "object"}) is None


async def test_record_run_units_per_work_item_writes_nothing_at_the_run():
    class _NoDb:
        def __getattr__(self, name):
            raise AssertionError(f"unexpected db.{name}")

    run = SimpleNamespace(id=None, agent_id=None, status="completed", result_summary="text")
    d = ResolvedDeclaration(unit="ticket_resolved", count_mode=COUNT_PER_WORK_ITEM)
    assert await roi_records.record_run_units(_NoDb(), run=run, declaration=d) is None
