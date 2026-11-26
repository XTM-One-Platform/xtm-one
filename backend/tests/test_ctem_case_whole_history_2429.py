# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""#2429: a case read derives its package, plan and review state from the whole history.

``get_ctem_case`` serves the newest 20 history entries; the threat package, the
treatment plan and the analyst review state it derives must not fall out with them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.ctem_case import CtemCaseEvent, CtemCaseIssue
from app.services import ctem_case_versions
from app.services import ctem_cases_view as view_of
from app.services.builtin_tools import ctem_cases as tools_module
from app.services.builtin_tools.ctem import build_ctem_tools
from app.tools.base import ToolCall
from tests.test_ctem_case_tools import AGENT, ISSUE, _case, env  # noqa: F401 (fixture)

GROUPING = "grouping--11111111-1111-4111-8111-111111111111"
COVERAGE = "coverage-7"
PAGE = "https://opencti.example/dashboard/analyses/groupings/" + GROUPING.split("--")[1]


def _row(case_id: Any, kind: str, at: datetime, **kw: Any) -> CtemCaseEvent:
    return CtemCaseEvent(case_id=case_id, actor="agent", kind=kind, at=at, **kw)


@pytest.fixture
def busy(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:  # noqa: F811
    """A case whose package, plan and review gate are older than 25 later events (notes, gates, versions)."""

    async def _none(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(ctem_case_versions, "for_case", _none)
    monkeypatch.setattr(ctem_case_versions, "inventory_sources", _none)

    async def _viewer(_db: Any, user_id: Any, vertical: Any, **_kw: Any) -> view_of.ViewerView:
        if str(user_id) == str(env.user_id):
            return view_of.ViewerView(
                issues={ISSUE.member_id: ISSUE}, open_ids={ISSUE.member_id}, opencti=True, opencrq=True
            )
        return view_of.ViewerView()

    monkeypatch.setattr(view_of, "viewer_view", _viewer)
    t0 = datetime.now(UTC) - timedelta(days=2)
    cid = env.case.id
    old = [
        _row(cid, "gate", t0, from_value=view_of.REVIEW_GATE, to_value="waiting", reason="analyst"),
        _row(cid, "treatment_plan", t0 + timedelta(minutes=1), to_value="plan-1", reason="Patch plan"),
        _row(cid, "threat_package", t0 + timedelta(minutes=2), from_value=COVERAGE, to_value=GROUPING, evidence=PAGE),
    ]
    later = [
        _row(cid, "versions" if i % 5 == 0 else "note", t0 + timedelta(minutes=10 + i), to_value=f"n{i}")
        for i in range(25)
    ]
    # The store answers the history newest first.
    env.db.added.extend(sorted(old + later, key=lambda e: e.at, reverse=True))

    async def _read(name: str, **args: Any) -> str:
        _, executor = build_ctem_tools(user_id=str(env.user_id), agent_id=AGENT)
        result = await executor.execute_tool(ToolCall(id="r1", name=name, arguments=args))
        return result.content if isinstance(result.content, str) else str(result.content)

    env.read = _read
    return env


async def _both(case: SimpleNamespace) -> list[dict[str, Any]]:
    read = await case.read("get_ctem_case", case_id=str(case.case.id))
    tools = await case.call("get_ctem_case", case_id=str(case.case.id))
    return [json.loads(read), json.loads(tools)]


async def test_a_package_older_than_the_page_still_reads_on_both_paths(busy: SimpleNamespace) -> None:
    for body in await _both(busy):
        package = body["threat_package"]
        assert package is not None and package["grouping_id"] == GROUPING
        assert package["coverage_id"] == COVERAGE
        # The review gate older than the page still stops the package.
        assert package["state"] == "awaiting_review"
        assert package["coverage_drafted"] is True and package["coverage_requested"] is False
        assert body["treatment_plan"]["id"] == "plan-1"
        assert "threat_package" not in body.get("empty_sections", {})
        assert "treatment_plan" not in body.get("empty_sections", {})
        # The served history stays one page, the versions record left out.
        assert len(body["history"]) <= tools_module._HISTORY_PAGE
        assert all(h["kind"] != view_of.VERSIONS_KIND for h in body["history"])
        assert not any(h["kind"] == "threat_package" for h in body["history"])


def test_hydrate_pages_only_the_history() -> None:
    case, t0 = _case(), datetime.now(UTC)
    events = [_row(case.id, "threat_package", t0, from_value=COVERAGE, to_value=GROUPING, evidence=PAGE)]
    events = [_row(case.id, "note", t0 + timedelta(minutes=i + 1)) for i in range(30)][::-1] + events
    rows = [CtemCaseIssue(case_id=case.id, source="opencti", external_id="vuln--1", open=True)]
    view = view_of.ViewerView(issues={ISSUE.member_id: ISSUE}, open_ids={ISSUE.member_id}, opencti=True)
    rendered = view_of.hydrate(case, rows, events, view, history_page=20)
    assert rendered is not None
    assert len(rendered["history"]) == 20
    assert rendered["threat_package"]["grouping_id"] == GROUPING
    # Without a page the whole history is served, as the case route reads it.
    assert len((view_of.hydrate(case, rows, events, view) or {})["history"]) == 31


def test_a_package_history_row_names_its_grouping_and_coverage() -> None:
    case = _case()
    row = _row(case.id, "threat_package", datetime.now(UTC), from_value=COVERAGE, to_value=GROUPING, evidence=PAGE)
    written = tools_module._written(row)
    assert written["grouping_id"] == GROUPING and written["coverage_id"] == COVERAGE and written["url"] == PAGE
    assert "from" not in written
    served = view_of._event(row, view_of.ViewerView(opencti=True))
    assert served["grouping_id"] == GROUPING and served["coverage_id"] == COVERAGE
    hidden = view_of._event(row, view_of.ViewerView())
    assert hidden["grouping_id"] is None and hidden["coverage_id"] is None
