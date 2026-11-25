"""CTEM case tools within their budget after a case-opening drain (#2092).

``update_ctem_case`` answers what it wrote, never the whole case re-read; an
identical gate decision is recorded once; a committed write never reports a
failure; ``get_ctem_case`` lists a bounded page of issues with the total; the
list tool never builds the whole view on the call.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.ctem_case import CtemCaseEvent, CtemCaseIssue
from app.services import ctem_cases as rules
from app.services import ctem_cases_store as store
from app.services import ctem_cases_view as view_of
from app.services.builtin_tools import ctem_cases as tools_module
from tests.test_ctem_case_tools import env  # noqa: F401 - fixture

GATE = {"key": "ctem.remediation.decision", "outcome": "passed_automatically", "by": "level low"}


@pytest.fixture
def history(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:  # noqa: F811
    """The case history is what the session added: a write's own events are read back."""

    async def _events(_db: Any, case_ids: list[Any]) -> dict[Any, list[Any]]:
        added = [r for r in env.db.added if isinstance(r, CtemCaseEvent)]
        return {cid: [e for e in added if e.case_id == cid] for cid in case_ids}

    for event in env.db.added:
        if isinstance(event, CtemCaseEvent) and event.id is None:
            event.id = uuid.uuid4()
    monkeypatch.setattr(store, "events_of", _events)
    original_add = env.db.add

    def _add(row: Any) -> None:
        if isinstance(row, CtemCaseEvent) and row.id is None:
            row.id = uuid.uuid4()
        original_add(row)

    env.db.add = _add
    return env


async def test_an_update_answers_what_it_wrote_not_the_whole_case(history: SimpleNamespace) -> None:
    body = json.loads(
        await history.call(
            "update_ctem_case",
            case_id=str(history.case.id),
            state="remediating",
            owner_user_id=str(history.user_id),
            note="patch window Friday",
        )
    )
    updated = body["updated"]
    assert updated["id"] == str(history.case.id) and updated["state"] == "remediating"
    assert updated["changed"] == ["note", "owner_user_id", "state"]
    assert {e["kind"] for e in updated["history_written"]} == {"owner", "note", "state"}
    # No issue list, no history page: get_ctem_case reads the case.
    assert "issues" not in updated and "history" not in updated


async def test_an_identical_gate_decision_is_recorded_once(history: SimpleNamespace) -> None:
    first = json.loads(await history.call("update_ctem_case", case_id=str(history.case.id), gate=dict(GATE)))
    again = json.loads(await history.call("update_ctem_case", case_id=str(history.case.id), gate=dict(GATE)))
    gates = [r for r in history.db.added if isinstance(r, CtemCaseEvent) and r.kind == "gate"]
    assert len(gates) == 1
    assert first["updated"]["changed"] == ["gate"] and again["updated"]["unchanged"] == ["gate"]
    other = json.loads(await history.call("update_ctem_case", case_id=str(history.case.id), gate={**GATE, "by": "Sam"}))
    assert other["updated"]["changed"] == ["gate"]
    assert len([r for r in history.db.added if isinstance(r, CtemCaseEvent) and r.kind == "gate"]) == 2


async def test_a_committed_write_never_reports_a_failure(
    history: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    real = store.events_of

    async def _events(db: Any, case_ids: list[Any]) -> dict[Any, list[Any]]:
        calls["n"] += 1
        if history.db.commits:
            raise RuntimeError("pool exhausted")
        return await real(db, case_ids)

    monkeypatch.setattr(store, "events_of", _events)
    body = json.loads(
        await history.call("update_ctem_case", case_id=str(history.case.id), note="checked", state="analyzing")
    )
    assert history.db.commits == 1
    assert body["updated"]["id"] == str(history.case.id) and body["updated"]["history_written"] == []
    assert not str(body).startswith("Error")


def test_a_large_case_read_lists_a_bounded_page_with_the_total() -> None:
    rendered = {"id": "c", "issues": [{"n": i} for i in range(737)], "member_count": 737}
    bounded = tools_module._bounded_issues(rendered)
    assert len(bounded["issues"]) == tools_module._ISSUE_PAGE
    assert bounded["issues_total"] == 737 and bounded["issues_truncated"] is True
    assert bounded["member_count"] == 737
    small = tools_module._bounded_issues({"issues": [{"n": 1}]})
    assert small["issues_truncated"] is False and small["issues_total"] == 1


async def test_get_reads_only_the_cases_members(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    seen: list[Any] = []

    async def _viewer(_db: Any, user_id: Any, vertical: str, *, fresh: bool = False, members=None, stale_ok=False):
        seen.append(members)
        issue = rules.SourceIssue(source="opencti", external_id="vuln--1", name="CVE-2026-1", cvss=9.8)
        return view_of.ViewerView(issues={issue.member_id: issue}, open_ids={issue.member_id})

    monkeypatch.setattr(view_of, "viewer_view", _viewer)
    body = json.loads(await env.call("get_ctem_case", case_id=str(env.case.id)))
    assert body["issues_total"] == 1 and seen and seen[0] == ["opencti:vuln--1"]


async def test_the_list_tool_says_when_the_view_is_rebuilding(
    env: SimpleNamespace,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _viewer(_db: Any, user_id: Any, vertical: str, *, fresh: bool = False, members=None, stale_ok=False):
        assert stale_ok, "the list reads with stale_ok"
        raise view_of.ViewRebuildingError(30)

    monkeypatch.setattr(view_of, "viewer_view", _viewer)
    answer = await env.call("list_ctem_cases")
    assert answer.startswith("Not available yet:") and "retry in 30 s" in answer


def test_the_member_ids_are_source_and_external_id() -> None:
    row = CtemCaseIssue(case_id=uuid.uuid4(), source="openaev", external_id="x-1", open=True)
    assert view_of._member_id(row) == "openaev:x-1"
