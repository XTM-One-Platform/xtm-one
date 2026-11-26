"""#2473: the validation and ticket writes answer compactly, never the whole case re-read.

``get_ctem_case`` pages its issues (#2092); ``record_ctem_case_validation`` and
``record_ctem_case_ticket`` answered the full unpaged rendering after their commit,
so a write on a large case cost more context than a read. They now answer the case
id, title and state after the write, whether it moved, what the call recorded and the
history it added.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.models.ctem_case import CtemCaseEvent, CtemCaseIssue
from app.services import ctem_case_loop
from app.services import ctem_cases as rules
from app.services import ctem_cases_store as store
from app.services import ctem_cases_view as view_of
from tests.test_ctem_case_tools import env  # noqa: F401  (the shared fixture)

ISSUES = 300


@pytest.fixture
def large(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:  # noqa: F811
    """The env case with 300 issues the acting user sees, and its written history readable."""
    issues = [
        rules.SourceIssue(source="opencti", external_id=f"vuln--{n}", name=f"CVE-2026-{n:04d} " + "x" * 200, cvss=7.5)
        for n in range(ISSUES)
    ]
    rows = [CtemCaseIssue(case_id=env.case.id, source=i.source, external_id=i.external_id, open=True) for i in issues]

    async def _get(_db: object, case_id: object, **_options: object) -> object:
        if case_id != env.case.id:
            return None
        return env.case, rows, [r for r in env.db.added if isinstance(r, CtemCaseEvent)]

    async def _viewer(
        _db: object,
        user_id: object,
        vertical: str,
        *,
        fresh: bool = False,
        members: object = None,
        stale_ok: bool = False,
    ) -> view_of.ViewerView:
        return view_of.ViewerView(issues={i.member_id: i for i in issues}, open_ids={i.member_id for i in issues})

    async def _events(_db: object, case_ids: list[object]) -> dict[object, list[object]]:
        added = [r for r in env.db.added if isinstance(r, CtemCaseEvent)]
        return {cid: [r for r in added if r.case_id == cid] for cid in case_ids}

    async def _publish(_pending: ctem_case_loop.PendingEvents) -> None:
        return None

    monkeypatch.setattr(store, "get_case", _get)
    monkeypatch.setattr(store, "events_of", _events)
    monkeypatch.setattr(view_of, "viewer_view", _viewer)
    monkeypatch.setattr(ctem_case_loop, "publish_pending", _publish)
    return env


async def test_the_full_read_is_larger_than_the_compact_write(large: SimpleNamespace) -> None:
    read = await large.call("get_ctem_case", case_id=str(large.case.id))
    assert json.loads(read)["issues_total"] == ISSUES and len(read) > 5000


async def test_a_validation_on_a_300_issue_case_answers_under_5000_characters(large: SimpleNamespace) -> None:
    answer = await large.call(
        "record_ctem_case_validation",
        case_id=str(large.case.id),
        provider="openaev",
        ref="simulation-7",
        result="pending",
    )
    assert len(answer) < 5000 and '"issues"' not in answer
    updated = json.loads(answer)["updated"]
    assert updated["id"] == str(large.case.id) and updated["state"] == large.case.state
    assert updated["validation"]["provider"] == "openaev" and updated["validation"]["ref"] == "simulation-7"
    assert updated["validation"]["result"] == "pending" and updated["validation"]["index"] == 0
    assert "validation" in {e["kind"] for e in updated["history_written"]}


async def test_a_passing_result_that_resolves_the_case_says_so(large: SimpleNamespace) -> None:
    answer = await large.call(
        "record_ctem_case_validation",
        case_id=str(large.case.id),
        provider="openaev",
        ref="simulation-8",
        result="passed",
        evidence_type="remediation",
    )
    assert len(answer) < 5000 and '"issues"' not in answer
    updated = json.loads(answer)["updated"]
    assert (updated["state"], updated["state_changed"], updated["previous_state"]) == ("resolved", True, "new")
    assert updated["resolution_kind"] == "fixed"
    assert updated["validation"]["evidence_type"] == "remediation"
    assert {"validation", "state"} <= {e["kind"] for e in updated["history_written"]}


async def test_a_ticket_on_a_300_issue_case_answers_compactly(large: SimpleNamespace) -> None:
    answer = await large.call(
        "record_ctem_case_ticket",
        case_id=str(large.case.id),
        integration="jira",
        key="ITS-12",
        url="https://j/ITS-12",
    )
    assert len(answer) < 5000 and '"issues"' not in answer
    updated = json.loads(answer)["updated"]
    assert updated["ticket"] == {
        "integration": "jira",
        "key": "ITS-12",
        "url": "https://j/ITS-12",
        "status": "open",
        "at": large.case.tickets[0]["at"],
    }
    assert (updated["state"], updated["state_changed"]) == ("remediating", True)


async def test_a_history_read_failing_after_the_commit_never_reports_a_failure(
    large: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    async def _flaky(_db: object, case_ids: list[object]) -> dict[object, list[object]]:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("database gone")
        return {cid: [] for cid in case_ids}

    monkeypatch.setattr(store, "events_of", _flaky)
    answer = json.loads(
        await large.call(
            "record_ctem_case_validation", case_id=str(large.case.id), provider="openaev", ref="s-9", result="pending"
        )
    )
    assert answer["updated"]["validation"]["ref"] == "s-9" and answer["updated"]["history_written"] == []
    assert large.db.commits == 1
