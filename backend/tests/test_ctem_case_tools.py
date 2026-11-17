"""CTEM case agent tools (#11545, slice S2): who is offered them and the round-trip.

The four tools act through the same store and rules as the API: an agent lists
and reads cases, splits issues into a manual case, changes owner and state with
a reason, is refused accepted risk, and resolves only with evidence.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.models.ctem_case import CtemCase, CtemCaseEvent, CtemCaseIssue
from app.services import ctem_cases as rules
from app.services import ctem_cases_store as store
from app.services import ctem_cases_view as view_of
from app.services.builtin_tools import CTEM_CASE_TOOL_NAMES, build_ctem_case_tools, offers_ctem_case_tools
from app.services.builtin_tools import ctem_cases as tools_module
from app.tools.base import ToolCall

ORG = uuid.uuid4()
AGENT = str(uuid.uuid4())
NOW = datetime.now(UTC)
ISSUE = rules.SourceIssue(source="opencti", external_id="vuln--1", name="CVE-2026-1", cvss=9.8)


def _case(**kw) -> CtemCase:
    base = {
        "id": uuid.uuid4(),
        "organization_id": ORG,
        "vertical": "cyber",
        "title": "Upgrade Apache Tomcat to 9.0.98",
        "remediation_kind": "upgrade",
        "keys": ["upgrade:apache/tomcat@9.0.98"],
        "pinned_issues": [],
        "state": "needs_attention",
        "state_since": NOW,
    }
    base.update(kw)
    return CtemCase(**base)


class _Db:
    def __init__(self):
        self.commits = 0
        self.added: list = []

    async def commit(self):
        self.commits += 1

    def add(self, row):
        self.added.append(row)

    async def get(self, _model, _id):
        return SimpleNamespace(ctem_vertical=None)


@pytest.fixture
def env(monkeypatch):
    """The acting user sees vuln--1; any other user sees nothing of the shared case."""
    seen = SimpleNamespace(db=_Db(), user_id=uuid.uuid4())
    seen.case = _case()
    rows = [CtemCaseIssue(case_id=seen.case.id, source="opencti", external_id="vuln--1", open=True)]
    seen.created = {}

    @asynccontextmanager
    async def _session():
        yield seen.db

    async def _primary(_db):
        return SimpleNamespace(id=ORG)

    async def _list(_db, *, organization_id, vertical):
        assert organization_id == ORG
        return [(seen.case, rows)]

    async def _get(_db, case_id, *, organization_id):
        if organization_id != ORG:
            return None
        if case_id in seen.created:
            return seen.created[case_id], rows, []
        if case_id != seen.case.id:
            return None
        events = [r for r in seen.db.added if isinstance(r, CtemCaseEvent)]
        return seen.case, rows, events

    async def _create(_db, **kw):
        seen.create_kw = kw
        if not set(kw["member_ids"]) <= kw["visible"]:
            raise rules.CaseRuleError("not an open issue of a case")
        case = _case(title=kw["title"], remediation_kind="manual", keys=[], pinned_issues=list(kw["member_ids"]))
        seen.created[case.id] = case
        return case

    async def _viewer(_db, user_id, vertical, *, fresh=False):
        if str(user_id) == str(seen.user_id):
            return view_of.ViewerView(issues={ISSUE.member_id: ISSUE}, open_ids={ISSUE.member_id})
        return view_of.ViewerView()

    monkeypatch.setattr("app.core.database.async_session", _session)
    monkeypatch.setattr("app.services.organizations.get_primary", _primary)
    monkeypatch.setattr(store, "list_cases", _list)
    monkeypatch.setattr(store, "get_case", _get)
    monkeypatch.setattr(store, "create_manual_case", _create)
    monkeypatch.setattr(view_of, "viewer_view", _viewer)

    def _tools(user_id: str):
        defs, executor = build_ctem_case_tools(user_id=user_id, agent_id=AGENT)

        async def call(name: str, **args) -> str:
            result = await executor.execute_tool(ToolCall(id="c1", name=name, arguments=args))
            return result.content if isinstance(result.content, str) else str(result.content)

        return defs, call

    seen.defs, seen.call = _tools(str(seen.user_id))
    seen.tools_for = _tools
    return seen


def test_only_the_ctem_remediation_agents_are_offered_the_tools(monkeypatch):
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)
    assert offers_ctem_case_tools("ctem-remediation-coordinator", is_builtin=True)
    assert offers_ctem_case_tools("ctem-mobilization-analyst", is_builtin=True)
    assert not offers_ctem_case_tools("ctem-remediation-coordinator", is_builtin=False)
    assert not offers_ctem_case_tools("ctem-assistant", is_builtin=True)
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: True), raising=False)
    assert not offers_ctem_case_tools("ctem-remediation-coordinator", is_builtin=True)


def test_the_four_tools_are_defined(env):
    assert tuple(d.name for d in env.defs) == CTEM_CASE_TOOL_NAMES


async def test_list_answers_the_cases_and_every_state_count(env):
    body = json.loads(await env.call("list_ctem_cases", state="needs_attention", limit=500))
    assert body["counts"] == {"needs_attention": 1, "in_progress": 0, "resolved": 0, "accepted_risk": 0}
    assert body["cases"][0]["title"] == "Upgrade Apache Tomcat to 9.0.98"
    assert body["cases"][0]["max_cvss"] == 9.8


async def test_an_agent_of_a_user_who_sees_no_issue_neither_lists_reads_nor_changes_the_case(env):
    """#11613: the tools render through the acting user's own source read."""
    _, other = env.tools_for(str(uuid.uuid4()))
    listed = json.loads(await other("list_ctem_cases"))
    assert listed["cases"] == [] and set(listed["counts"].values()) == {0}
    assert "No case" in await other("get_ctem_case", case_id=str(env.case.id))
    assert "No case" in await other("update_ctem_case", case_id=str(env.case.id), state="in_progress")
    assert env.case.state == "needs_attention" and env.db.commits == 0


async def test_an_agent_cannot_name_an_owner_who_sees_none_of_the_issues(env):
    answer = await env.call(
        "update_ctem_case", case_id=str(env.case.id), state="in_progress", owner_user_id=str(uuid.uuid4())
    )
    assert answer.startswith("Refused: the owner must be able to see") and env.db.commits == 0


async def test_without_an_acting_user_there_is_no_case(env):
    _, anonymous = env.tools_for("")
    assert "no cases" in await anonymous("list_ctem_cases")
    assert "No case" in await anonymous("get_ctem_case", case_id=str(env.case.id))


async def test_get_shows_where_the_issues_came_from(env):
    body = json.loads(await env.call("get_ctem_case", case_id=str(env.case.id)))
    assert (body["issues"][0]["source"], body["issues"][0]["external_id"]) == ("opencti", "vuln--1")
    assert "No case" in await env.call("get_ctem_case", case_id=str(uuid.uuid4()))


async def test_an_agent_starts_a_case_and_the_event_names_it(env):
    owner = str(env.user_id)
    body = json.loads(
        await env.call("update_ctem_case", case_id=str(env.case.id), state="in_progress", owner_user_id=owner)
    )
    assert body["updated"]["state"] == "in_progress" and body["updated"]["owner_user_id"] == owner
    events = [r for r in env.db.added if isinstance(r, CtemCaseEvent) and r.kind == "state"]
    assert events and events[0].actor == AGENT and env.db.commits == 1


async def test_an_agent_never_accepts_risk(env):
    answer = await env.call(
        "update_ctem_case",
        case_id=str(env.case.id),
        state="accepted_risk",
        owner_user_id=str(env.user_id),
        reason="compensating control",
    )
    assert answer.startswith("Refused: only a person accepts risk")
    assert env.case.state == "needs_attention" and env.db.commits == 0


async def test_resolving_needs_evidence(env):
    answer = await env.call("update_ctem_case", case_id=str(env.case.id), state="resolved")
    assert answer.startswith("Refused: resolving a case needs evidence")
    body = json.loads(
        await env.call("update_ctem_case", case_id=str(env.case.id), state="resolved", evidence="CHG-1234 deployed")
    )
    assert body["updated"]["state"] == "resolved"


async def test_create_splits_issues_into_a_manual_case(env):
    body = json.loads(
        await env.call("create_ctem_case", title="Tomcat on the DMZ hosts", issue_ids=["opencti:vuln--1"], reason="x")
    )
    assert body["created"]["remediation_kind"] == "manual"
    assert env.create_kw["member_ids"] == ["opencti:vuln--1"] and env.create_kw["actor"] == AGENT
    assert env.create_kw["visible"] == {"opencti:vuln--1"}
    assert env.db.commits == 1


async def test_create_refuses_an_issue_the_acting_user_cannot_see(env):
    answer = await env.call("create_ctem_case", title="Steal", issue_ids=["opencti:vuln--2"])
    assert answer.startswith("Refused: not an open issue of a case") and env.db.commits == 0


async def test_bad_arguments_are_answered_without_a_write(env):
    assert "case_id must be" in await env.call("update_ctem_case", case_id="nope")
    assert "Unknown state" in await env.call("update_ctem_case", case_id=str(env.case.id), state="closed")
    assert "due_at must be" in await env.call("update_ctem_case", case_id=str(env.case.id), due_at="tomorrow")
    assert env.db.commits == 0


def test_module_is_wired_to_the_tool_names():
    slugs = set(tools_module.CTEM_CASE_AGENT_SLUGS)
    assert slugs == {"ctem-remediation-coordinator", "ctem-mobilization-analyst"}
