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
from typing import Any

import pytest

from app.core.config import settings
from app.models.ctem_case import CASE_STATES, CtemCase, CtemCaseEvent, CtemCaseIssue
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
        "state": "new",
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

    async def _get(_db: object, case_id: object, *, organization_id: object, **_options: object) -> object:
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

    async def _viewer(_db, user_id, vertical, *, fresh=False, members=None, stale_ok=False):
        if str(user_id) == str(seen.user_id):
            return view_of.ViewerView(issues={ISSUE.member_id: ISSUE}, open_ids={ISSUE.member_id})
        return view_of.ViewerView()

    monkeypatch.setattr("app.core.database.async_session", _session)
    monkeypatch.setattr("app.services.organizations.get_primary", _primary)
    monkeypatch.setattr(store, "list_cases", _list)
    monkeypatch.setattr(store, "get_case", _get)
    monkeypatch.setattr(store, "create_manual_case", _create)

    # No case of these tests was merged away (#2289): the merge lookups answer none.
    async def _merged_into(_db: object, case_id: object, *, organization_id: object) -> object:
        return seen.merged.get(case_id)

    async def _merged_with_prefix(_db: object, prefix: str, *, organization_id: object, limit: int = 2) -> list[object]:
        return [m for m in seen.merged.values() if str(m.case_id).startswith(prefix)][:limit]

    seen.merged = {}
    monkeypatch.setattr(store, "merged_into", _merged_into)
    monkeypatch.setattr(store, "merged_with_prefix", _merged_with_prefix)

    async def _events(_db: object, case_ids: list[object]) -> dict[object, list[object]]:
        return {cid: [] for cid in case_ids}

    # The list's asset count and the read's version history read the case events (#1694).
    monkeypatch.setattr(store, "events_of", _events)
    monkeypatch.setattr(view_of, "viewer_view", _viewer)

    async def _never_deleted(_db: object, _case_id: object) -> None:
        return None

    # No delete audit row in this fake session (#2328): a miss stays "No case".
    monkeypatch.setattr("app.services.ctem_case_delete.deleted_case_answer", _never_deleted)

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
    assert offers_ctem_case_tools("ctem-vuln-prioritization-orchestrator", is_builtin=True)
    assert offers_ctem_case_tools("ctem-threat-response-orchestrator", is_builtin=True)
    assert not offers_ctem_case_tools("ctem-remediation-coordinator", is_builtin=False)
    assert not offers_ctem_case_tools("ctem-assistant", is_builtin=True)
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: True), raising=False)
    assert not offers_ctem_case_tools("ctem-remediation-coordinator", is_builtin=True)


def test_the_case_tools_are_defined(env: SimpleNamespace) -> None:
    assert tuple(d.name for d in env.defs) == CTEM_CASE_TOOL_NAMES


async def test_list_answers_the_cases_and_every_state_count(env):
    body = json.loads(await env.call("list_ctem_cases", state="new", limit=500))
    assert body["counts"] == {state: int(state == "new") for state in CASE_STATES}
    assert body["cases"][0]["title"] == "Upgrade Apache Tomcat to 9.0.98"
    assert body["cases"][0]["max_cvss"] == 9.8


async def test_an_agent_of_a_user_who_sees_no_issue_neither_lists_reads_nor_changes_the_case(env):
    """#11613: the tools render through the acting user's own source read."""
    _, other = env.tools_for(str(uuid.uuid4()))
    listed = json.loads(await other("list_ctem_cases"))
    assert listed["cases"] == [] and set(listed["counts"].values()) == {0}
    assert "No case" in await other("get_ctem_case", case_id=str(env.case.id))
    assert "No case" in await other("update_ctem_case", case_id=str(env.case.id), state="remediating")
    assert env.case.state == "new" and env.db.commits == 0


async def test_an_agent_cannot_name_an_owner_who_sees_none_of_the_issues(env):
    answer = await env.call(
        "update_ctem_case", case_id=str(env.case.id), state="remediating", owner_user_id=str(uuid.uuid4())
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
        await env.call("update_ctem_case", case_id=str(env.case.id), state="remediating", owner_user_id=owner)
    )
    assert body["updated"]["state"] == "remediating" and body["updated"]["owner_user_id"] == owner
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
    assert env.case.state == "new" and env.db.commits == 0


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
    assert slugs == {
        "ctem-remediation-coordinator",
        "ctem-mobilization-analyst",
        "ctem-vuln-prioritization-orchestrator",
        "ctem-threat-response-orchestrator",
    }


# ── #891: the validation and ticket references an agent writes on a case ──


async def test_a_passing_retest_resolves_the_case_and_raises_resolved(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import ctem_case_loop

    published: list[str] = []

    async def _publish(pending: ctem_case_loop.PendingEvents) -> None:
        published.extend(name for name, _data, _id in pending.items)

    monkeypatch.setattr(ctem_case_loop, "publish_pending", _publish)
    body = json.loads(
        await env.call(
            "record_ctem_case_validation",
            case_id=str(env.case.id),
            provider="openaev",
            ref="simulation-42",
            result="passed",
            evidence_type="remediation",
        )
    )
    assert body["updated"]["state"] == "resolved"
    assert env.case.validations[-1]["ref"] == "simulation-42" and env.case.validations[-1]["kind"] == "retest"
    assert ctem_case_loop.CASE_RESOLVED in published and env.db.commits == 1
    kinds = {r.kind for r in env.db.added if isinstance(r, CtemCaseEvent)}
    assert {"validation", "state"} <= kinds


async def test_the_agents_conclusive_is_stored_as_given_and_no_class_is_derived(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1810: the agent's conclusive is stored as given; safe_mode and inject_class are no longer arguments."""
    from app.services import ctem_case_loop

    async def _publish(_pending: ctem_case_loop.PendingEvents) -> None:
        return None

    monkeypatch.setattr(ctem_case_loop, "publish_pending", _publish)
    body = json.loads(
        await env.call(
            "record_ctem_case_validation",
            case_id=str(env.case.id),
            provider="openaev",
            ref="inject-7",
            result="not_exposed",
            conclusive=False,
        )
    )
    assert body["updated"]["state"] == "new" and "inconclusive" in body["inconclusive"]
    entry = env.case.validations[-1]
    assert entry["conclusive"] is False
    assert "safe_mode" not in entry and "inject_class" not in entry and "warning" not in body


async def test_a_requested_id_with_no_template_is_never_recorded_negative(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import ctem_case_loop

    async def _publish(_pending: ctem_case_loop.PendingEvents) -> None:
        return None

    monkeypatch.setattr(ctem_case_loop, "publish_pending", _publish)
    body = json.loads(
        await env.call(
            "record_ctem_case_validation",
            case_id=str(env.case.id),
            provider="openaev",
            ref="inject-8",
            result="not_exposed",
            not_tested=["CVE-2026-2"],
        )
    )
    assert body["updated"]["state"] == "new" and body["not_tested"] == ["CVE-2026-2"]
    entry = env.case.validations[-1]
    assert entry["conclusive"] is False and "not tested" in entry["note"]


async def test_a_validation_without_its_reference_is_refused(env: SimpleNamespace) -> None:
    answer = await env.call(
        "record_ctem_case_validation", case_id=str(env.case.id), provider="openaev", ref="", result="passed"
    )
    assert answer.startswith("Refused:") and env.db.commits == 0
    assert "No case" in await env.call(
        "record_ctem_case_validation", case_id=str(uuid.uuid4()), provider="openaev", ref="x", result="passed"
    )


async def test_a_ticket_reference_is_written_once_and_updated_in_place(env: SimpleNamespace) -> None:
    first = json.loads(
        await env.call(
            "record_ctem_case_ticket",
            case_id=str(env.case.id),
            integration="jira",
            key="ITS-12",
            url="https://j/ITS-12",
        )
    )
    assert first["updated"]["state"] == "remediating"  # a ticket in flight: the remediation is under way (#1503)
    await env.call("record_ctem_case_ticket", case_id=str(env.case.id), integration="jira", key="ITS-12", status="done")
    assert len(env.case.tickets) == 1
    assert (env.case.tickets[0]["status"], env.case.tickets[0]["url"]) == ("done", "https://j/ITS-12")
    # The same write again changes nothing and writes no event.
    before = len(env.db.added)
    await env.call("record_ctem_case_ticket", case_id=str(env.case.id), integration="jira", key="ITS-12", status="done")
    assert len(env.db.added) == before
    assert (await env.call("record_ctem_case_ticket", case_id=str(env.case.id), integration="jira", key="")).startswith(
        "Refused:"
    )
    assert (
        await env.call("record_ctem_case_ticket", case_id=str(env.case.id), integration="jira", key="A", status="x")
    ).startswith("Refused:")


async def test_the_ticket_sync_tool_goes_through_the_layer(env: SimpleNamespace, monkeypatch) -> None:
    """#917 slice 4: sync_ctem_case_ticket calls ctem_ticketing.sync_one for the case the user sees."""
    from app.services import ctem_case_loop, ctem_ticketing

    seen: dict = {}

    async def _sync_one(db, case, *, create=False, events=None, base_url="", now=None):
        seen.update(case=case.id, create=create)
        return {"connected": False, "reason": ctem_ticketing.NOT_CONNECTED, "settings": ctem_ticketing.SETTINGS_PATH}

    async def _publish(_pending) -> None:
        return None

    monkeypatch.setattr(ctem_ticketing, "sync_one", _sync_one)
    monkeypatch.setattr(ctem_case_loop, "publish_pending", _publish)
    body = json.loads(await env.call("sync_ctem_case_ticket", case_id=str(env.case.id), create=True))
    assert body["connected"] is False and body["reason"] == ctem_ticketing.NOT_CONNECTED
    assert seen == {"case": env.case.id, "create": True}
    assert "No case" in await env.call("sync_ctem_case_ticket", case_id=str(uuid.uuid4()))
    _, other = env.tools_for(str(uuid.uuid4()))
    assert "No case" in await other("sync_ctem_case_ticket", case_id=str(env.case.id))


async def test_a_gate_pass_is_written_on_the_case_and_an_unknown_gate_is_refused(env: SimpleNamespace) -> None:
    """CTEM-34 (#916): the loop trace shows every gate, passed automatically or waited."""
    body = json.loads(
        await env.call(
            "update_ctem_case",
            case_id=str(env.case.id),
            gate={"key": "ctem.remediation.decision", "outcome": "passed_automatically", "by": "level low"},
        )
    )
    assert body["updated"]["state"] == "remediating"  # the decision gate passed: remediation decided (#1503)
    gates = [r for r in env.db.added if isinstance(r, CtemCaseEvent) and r.kind == "gate"]
    assert [(g.from_value, g.to_value, g.reason) for g in gates] == [
        ("ctem.remediation.decision", "passed_automatically", "level low")
    ]
    commits = env.db.commits
    refused = await env.call(
        "update_ctem_case", case_id=str(env.case.id), gate={"key": "ticket.create", "outcome": "waited"}
    )
    assert refused.startswith("Refused:") and env.db.commits == commits


@pytest.mark.parametrize(
    ("gate", "prefix"),
    [
        ("ctem.analyst_review", "ctem:review:"),
        ("ctem.validation.non_readonly", "ctem:validation:"),
        ("ctem.validation.non_readonly", "ctem:f2:validate:"),
        ("ctem.remediation.decision", "ctem:remediation:"),
        ("ctem.mobilize", "ctem:f1:case-mobilize:"),
        ("ctem.mobilize", "ctem:f2:mobilize:"),
        ("ctem.accept_risk", "ctem:accept-risk:"),
    ],
)
async def test_a_waited_gate_needs_its_open_decision_item(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, gate: str, prefix: str
) -> None:
    """#1954: a gate reaches waited only through its decision; without one the write is refused, nothing written."""
    from app.services import ctem_gate_order

    open_keys: set[str] = set()

    async def _open(_db: object, case_id: str) -> set[str]:
        assert case_id == str(env.case.id)
        return set(open_keys)

    monkeypatch.setattr(ctem_gate_order, "open_gate_item_keys", _open)
    commits = env.db.commits
    refused = await env.call(
        "update_ctem_case", case_id=str(env.case.id), state="analyzing", gate={"key": gate, "outcome": "waited"}
    )
    assert refused.startswith("Refused:") and "create_work_item" in refused
    assert env.db.commits == commits and env.db.added == []
    # Another gate's decision is not this gate's decision.
    open_keys.add(f"ctem:other:{uuid.uuid4()}")
    assert (
        await env.call("update_ctem_case", case_id=str(env.case.id), gate={"key": gate, "outcome": "waited"})
    ).startswith("Refused:")
    open_keys.add(prefix + str(env.case.id))
    body = json.loads(
        await env.call("update_ctem_case", case_id=str(env.case.id), gate={"key": gate, "outcome": "waited"})
    )
    assert "updated" in body
    written = [r for r in env.db.added if isinstance(r, CtemCaseEvent) and r.kind == "gate"]
    assert [(g.from_value, g.to_value) for g in written] == [(gate, "waited")]


async def test_a_pass_or_an_answer_needs_no_decision_item(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1954: only waited rests on a decision item; a pass or an answer is written as before."""
    from app.services import ctem_gate_order

    async def _never(_db: object, _case_id: str) -> set[str]:
        raise AssertionError("read only for a waited gate")

    monkeypatch.setattr(ctem_gate_order, "open_gate_item_keys", _never)
    for outcome in ("passed_automatically", "answered"):
        body = json.loads(
            await env.call(
                "update_ctem_case", case_id=str(env.case.id), gate={"key": "ctem.analyst_review", "outcome": outcome}
            )
        )
        assert "updated" in body


_REMEDIATION_OPTIONS = [
    {"key": "A", "label": "Disable HTTP/2", "cost": 6000, "expected_loss_before": 500, "expected_loss_after": 50},
    {"key": "B", "label": "Upgrade", "cost": 45000, "expected_loss_before": 500, "expected_loss_after": 10},
]


def _remediation_env(
    monkeypatch: pytest.MonkeyPatch, *, pin: str | None, threshold: float | None, opencrq: bool = True
) -> list[dict[str, Any]]:
    """CTEM-17: the fleet catalog, the profile threshold, OpenCRQ access and the work item writer, faked."""
    from app.services import fleet_autonomy, fleet_runs, organization_context, organization_context_opencrq
    from app.services import work_item as work_item_module

    fleet_id = uuid.uuid4()

    async def _homes(_db: object, _agents: object) -> dict[uuid.UUID, SimpleNamespace]:
        return {uuid.UUID(AGENT): SimpleNamespace(fleet_id=fleet_id)}

    async def _autonomy(_db: object, _fleet: object) -> tuple[str, list[dict[str, str]]]:
        gate: dict[str, str] = {"key": "ctem.remediation.decision", "impact": "high"}
        if pin:
            gate["pin"] = pin
        return "low", [gate]

    async def _connection(_db: object, _user: object) -> tuple[str, dict[str, str]] | str:
        return ("https://crq", {}) if opencrq else "no access"

    async def _version(_db: object, _org: object = None) -> SimpleNamespace:
        return SimpleNamespace(profile={"risk_appetite": {"approval_threshold": threshold}})

    created: list[dict[str, Any]] = []

    async def _create(**kw: Any) -> dict[str, str]:
        created.append(kw)
        return {"id": "item-1"}

    monkeypatch.setattr(fleet_runs, "home_fleet_defaults_of", _homes)
    monkeypatch.setattr(fleet_autonomy, "fleet_autonomy_for_run", _autonomy)
    monkeypatch.setattr(organization_context_opencrq, "_opencrq_connection", _connection)
    monkeypatch.setattr(organization_context, "current_version", _version)
    monkeypatch.setattr(work_item_module, "create_work_item", _create)
    return created


async def test_the_remediation_decision_is_decided_by_policy_out_of_the_box(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _remediation_env(monkeypatch, pin=None, threshold=None)
    body = json.loads(
        await env.call(
            "decide_ctem_case_remediation", case_id=str(env.case.id), options=_REMEDIATION_OPTIONS, recommendation="A"
        )
    )
    assert body["decided"] is True and body["option"] == "A" and created == []
    gates = [r for r in env.db.added if isinstance(r, CtemCaseEvent) and r.kind == "gate"]
    assert [(g.from_value, g.to_value) for g in gates] == [("ctem.remediation.decision", "passed_automatically")]
    assert env.db.commits == 1


async def test_the_remediation_decision_waits_above_a_set_threshold_as_one_unified_decision(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _remediation_env(monkeypatch, pin=None, threshold=100)
    body = json.loads(
        await env.call(
            "decide_ctem_case_remediation", case_id=str(env.case.id), options=_REMEDIATION_OPTIONS, recommendation="A"
        )
    )
    assert body["decided"] is False and body["decision_item_id"] == "item-1"
    (item,) = created
    assert item["needs_human"] is True and item["ask"]["recommendation"] == "A"
    assert item["correlation_key"] == f"ctem:remediation:{env.case.id}"
    gates = [r for r in env.db.added if isinstance(r, CtemCaseEvent) and r.kind == "gate"]
    assert [g.to_value for g in gates] == ["waited"]
    assert "expected loss 500" in body["reason"]
    trail = [r.reason or "" for r in env.db.added if isinstance(r, CtemCaseEvent)]
    assert trail and all("500" not in t and "cost" not in t and "threshold 100" not in t for t in trail)


async def test_a_decider_without_opencrq_read_gets_no_figures_on_the_waiting_decision(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _remediation_env(monkeypatch, pin="wait", threshold=None, opencrq=False)
    body = json.loads(
        await env.call(
            "decide_ctem_case_remediation", case_id=str(env.case.id), options=_REMEDIATION_OPTIONS, recommendation="B"
        )
    )
    assert body["figures_shown"] is False
    assert all("expected loss" not in o["effect"] for o in created[0]["ask"]["options"])


async def test_a_person_answer_is_written_on_the_case(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    _remediation_env(monkeypatch, pin="wait", threshold=None)
    body = json.loads(
        await env.call(
            "decide_ctem_case_remediation",
            case_id=str(env.case.id),
            options=_REMEDIATION_OPTIONS,
            recommendation="A",
            answered_by="user-1",
            answered_option="B",
        )
    )
    assert body == {"decided": True, "option": "B", "by": "user-1"}
    gates = [r for r in env.db.added if isinstance(r, CtemCaseEvent) and r.kind == "gate"]
    assert [(g.to_value, g.reason, g.evidence) for g in gates] == [("answered", "user-1", "B")]


# ── #2037: a duplicate is merged by the loop, never deleted ──


async def test_an_agent_merges_a_duplicate_into_the_case_it_names(env: SimpleNamespace, monkeypatch) -> None:
    duplicate = _case(title="Upgrade windows to a fixed version", keys=["upgrade:-/windows@latest"])
    env.created[duplicate.id] = duplicate
    calls: list[dict[str, Any]] = []

    async def _merge(_db, **kw):
        calls.append(kw)
        return env.case

    monkeypatch.setattr(store, "merge_cases", _merge)
    body = json.loads(
        await env.call("merge_ctem_cases", target_id=str(env.case.id), case_ids=[str(duplicate.id)], reason="same fix")
    )
    assert body["merged"] == [str(duplicate.id)] and body["into"]["id"] == str(env.case.id)
    assert calls[0]["actor"] == AGENT and calls[0]["source_ids"] == [duplicate.id]
    assert calls[0]["target_id"] == env.case.id and calls[0]["reason"] == "same fix"
    assert env.db.commits == 1


async def test_a_merge_of_a_case_the_agent_does_not_see_is_refused(env: SimpleNamespace, monkeypatch) -> None:
    async def _merge(_db, **kw):
        raise AssertionError("no merge on a case the acting person does not see")

    monkeypatch.setattr(store, "merge_cases", _merge)
    unseen = str(uuid.uuid4())
    assert "No case" in await env.call("merge_ctem_cases", target_id=str(env.case.id), case_ids=[unseen])
    _, other = env.tools_for(str(uuid.uuid4()))
    assert "No case" in await other("merge_ctem_cases", target_id=str(env.case.id), case_ids=[unseen])
    assert "must be ids" in await env.call("merge_ctem_cases", target_id=str(env.case.id), case_ids=[])
    assert env.db.commits == 0


async def test_a_merge_rule_refusal_writes_nothing(env: SimpleNamespace, monkeypatch) -> None:
    async def _merge(_db, **kw):
        raise rules.CaseRuleError("the surviving case cannot also be merged into itself")

    monkeypatch.setattr(store, "merge_cases", _merge)
    answer = await env.call("merge_ctem_cases", target_id=str(env.case.id), case_ids=[str(env.case.id)])
    assert answer.startswith("Refused: the surviving case") and env.db.commits == 0


def test_a_case_names_the_simulations_its_derived_items_are_matched_on() -> None:
    """#2389: the delete cascade reads the case's validation references and attack-path hops."""
    from app.services import ctem_case_delete as case_delete

    case = _case(
        validations=[{"provider": "openaev", "ref": "sim-1"}, {"provider": "openaev", "ref": "sim-1"}, {"ref": ""}],
        attack_path={"hops": [{"simulation": "sim-2"}, {"simulation": None}]},
    )
    assert case_delete.case_simulations(case) == ["sim-1", "sim-2"]
    assert case_delete.case_simulations(_case()) == []


def test_delete_points_to_merge_for_duplicates(env: SimpleNamespace) -> None:
    described = {d.name: d.description for d in env.defs}
    assert "reopens at the next reconcile" in described["delete_ctem_cases"]
    assert "merge duplicates" in described["delete_ctem_cases"]
    assert "never reopens" in described["merge_ctem_cases"]
