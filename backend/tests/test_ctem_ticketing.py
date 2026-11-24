"""Two-way ticketing sync for CTEM cases (#917): contract, idempotency, status mapping.

``services/ctem_ticketing.py`` with a fake adapter and a fake session: one
ticket per case whatever the replays, the status mapping both ways, the sync
states (in sync, out of date with its since, error with its fix) and the
re-test asked when a ticket reaches done.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ctem_case import CtemCase
from app.services import ctem_ticketing as tk
from app.services.organization_context import TicketingSync, TicketingTarget

NOW = datetime(2026, 11, 21, 16, 0, tzinfo=UTC)


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)


class FakeAdapter:
    provider = "jira"

    def __init__(self) -> None:
        self.tickets: dict[str, tk.RemoteTicket] = {}
        self.by_correlation: dict[str, str] = {}
        self.created = 0
        self.fail: str | None = None
        self.comments: list[tuple[str, str]] = []

    def _check(self) -> None:
        if self.fail:
            raise tk.TicketingError(self.fail, fix="reconnect", fix_code="reconnect_jira")

    async def find(self, correlation: str):
        self._check()
        key = self.by_correlation.get(correlation)
        return self.tickets.get(key) if key else None

    async def create(self, draft: tk.TicketDraft):
        self._check()
        self.created += 1
        key = f"ITS-{self.created}"
        ticket = tk.RemoteTicket(key=key, url=f"https://jira/browse/{key}", status="open", external_status="To Do")
        self.tickets[key] = ticket
        self.by_correlation[draft.correlation] = key
        return ticket

    async def update(self, key: str, draft: tk.TicketDraft, *, status: str | None = None):
        self._check()
        if status:
            self.tickets[key] = tk.RemoteTicket(
                key=key, url=self.tickets[key].url, status=status, external_status=status
            )
        return self.tickets[key]

    async def comment(self, key: str, text: str) -> None:
        self.comments.append((key, text))

    async def read_status(self, key: str):
        self._check()
        return self.tickets[key]


def _case(**kw) -> CtemCase:
    base = dict(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vertical="cyber",
        title="Close the exposure of admin.example.com",
        remediation_kind="close_exposure",
        keys=["close_exposure:asset/admin.example.com"],
        pinned_issues=[],
        state="remediating",
        state_since=NOW,
        trigger_path="new_exposure",
        risk_scenario_ids=[],
        severity="P1",
        sla_due_at=NOW + timedelta(hours=24),
        sla_breached_at=None,
        tickets=[],
        validations=[],
        validation_plan=True,
        resolution_kind=None,
        fixed_version=None,
    )
    base.update(kw)
    return CtemCase(**base)


SYNC = TicketingSync()


@pytest.mark.asyncio
async def test_one_ticket_per_case_whatever_the_replays():
    db, adapter, case = FakeSession(), FakeAdapter(), _case()
    first = await tk.ensure_ticket(db, case, adapter, SYNC, integration="jira", now=NOW)
    assert first.created and adapter.created == 1
    again = await tk.ensure_ticket(db, case, adapter, SYNC, integration="jira", now=NOW)
    assert not again.created and adapter.created == 1
    # A retried run that lost the case write finds the ticket again by its correlation.
    fresh = _case(id=case.id)
    found = await tk.ensure_ticket(db, fresh, adapter, SYNC, integration="jira", now=NOW)
    assert not found.created and adapter.created == 1
    assert fresh.tickets[0]["key"] == "ITS-1"
    assert fresh.tickets[0]["sync"]["state"] == "in_sync"
    assert fresh.tickets[0]["sync"]["correlation"] == tk.correlation_of(case)


def test_draft_maps_title_priority_due_date_labels_and_link():
    case = _case(severity="P2")
    draft = tk.draft_of(case, TicketingSync(labels=["security"]), base_url="https://xtm.example/")
    assert draft.title.startswith("[P2] ")
    assert draft.priority == "High"
    assert draft.due_date == (NOW + timedelta(hours=24)).date().isoformat()
    assert draft.labels == ("security", "xtm-ctem")
    assert draft.link == f"https://xtm.example/xtm-one/ctem/cases/{case.id}"
    assert tk.correlation_of(case) in draft.description
    custom = tk.draft_of(case, TicketingSync(priority_map={"P2": "Major"}))
    assert custom.priority == "Major"
    assert tk.draft_of(case, SYNC, provider="servicenow").priority == "2"


@pytest.mark.parametrize(
    ("name", "category", "custom", "expected"),
    [
        ("To Do", "new", None, "open"),
        ("In Progress", "indeterminate", None, "in_progress"),
        ("Done", "done", None, "done"),
        ("Fixed in prod", "", {"fixed in prod": "done"}, "done"),
        ("Won't Do", "done", {"Won't Do": "cancelled"}, "cancelled"),
        ("1", "", None, "open"),
        ("2", "", None, "in_progress"),
        ("6", "", None, "done"),
        ("8", "", None, "cancelled"),
        ("Awaiting vendor", "", None, "open"),
    ],
)
def test_status_mapping_customer_map_first_then_categories(name, category, custom, expected):
    assert tk.map_status(name, status_map=custom, category=category) == expected


@pytest.mark.parametrize(
    ("table", "state", "expected"),
    [
        ("incident", "6", "done"),
        ("incident", "3", "in_progress"),
        ("change_request", "3", "done"),
        ("change_request", "4", "cancelled"),
        ("change_request", "-1", "in_progress"),
        ("change_request", "-5", "open"),
        ("problem", "106", "done"),
        ("problem", "101", "open"),
    ],
)
def test_servicenow_states_are_read_per_table(table, state, expected):
    assert tk.map_status(state, table=table) == expected


def _servicenow(table: str) -> tuple[tk.ServiceNowAdapter, list[tuple[str, dict]]]:
    adapter = tk.ServiceNowAdapter("https://sn.example", {}, table)
    calls: list[tuple[str, dict]] = []

    async def _call(method, url, action, **kwargs):
        calls.append((method, kwargs))
        if method == "get":
            return [{"sys_id": "abc", "number": "CHG001", "state": "-1"}]
        return {"sys_id": "abc", "number": "CHG001", "state": kwargs["json"].get("state", "")}

    adapter._call = _call  # type: ignore[method-assign]
    return adapter, calls


@pytest.mark.parametrize(
    ("table", "status", "written"),
    [
        ("change_request", "done", "3"),
        ("change_request", "cancelled", "4"),
        ("incident", "done", "6"),
        ("problem", "done", "106"),
    ],
)
async def test_servicenow_writes_the_state_of_its_table(table, status, written):
    adapter, calls = _servicenow(table)
    draft = tk.draft_of(_case(severity="P1"), SYNC, provider="servicenow")
    remote = await adapter.update("CHG001", draft, status=status)
    assert calls[-1][0] == "patch"
    assert calls[-1][1]["json"]["state"] == written
    assert remote.status == status


def test_servicenow_refuses_a_table_it_does_not_map():
    with pytest.raises(tk.TicketingError):
        tk.ServiceNowAdapter("https://sn.example", {}, "sn_vul_vulnerable_item")


class _IntegrationSession:
    def __init__(self, provider: str) -> None:
        self.provider = provider

    async def get(self, model, key):
        return type("Integration", (), {"provider": self.provider})()


def _profile(record_type: str):
    from app.services.organization_context import OrganizationProfile

    return OrganizationProfile(
        ticketing=TicketingTarget(integration=str(uuid.uuid4()), sync=TicketingSync(record_type=record_type))
    )


async def test_settings_save_refuses_an_unknown_servicenow_table_only():
    problem = await tk.target_problem(_IntegrationSession("servicenow"), _profile("sn_vul_vulnerable_item"))
    assert problem and "record_type" in problem
    assert await tk.target_problem(_IntegrationSession("servicenow"), _profile("change_request")) is None
    assert await tk.target_problem(_IntegrationSession("servicenow"), _profile("")) is None
    assert await tk.target_problem(_IntegrationSession("jira"), _profile("Bug")) is None


def test_create_rule_follows_the_settings():
    assert tk.should_create(_case(severity="P1"), SYNC)
    assert tk.should_create(_case(severity="P2"), SYNC)
    assert not tk.should_create(_case(severity="P3"), SYNC)
    assert not tk.should_create(_case(severity="P2"), TicketingSync(create_on="mobilize_p1"))
    assert not tk.should_create(_case(severity="P1"), TicketingSync(create_on="manual"))


@pytest.mark.asyncio
async def test_ticket_done_asks_the_retest_once_and_never_resolves_the_case():
    db, adapter, case = FakeSession(), FakeAdapter(), _case()
    await tk.ensure_ticket(db, case, adapter, SYNC, integration="jira", now=NOW)
    adapter.tickets["ITS-1"] = tk.RemoteTicket(key="ITS-1", url="u", status="done", external_status="Done")
    outcome = await tk.pull_ticket_status(db, case, adapter, SYNC, now=NOW)
    assert outcome.retest_requested
    assert case.state == "validating"  # the ticket done hands the case to its re-test (#1503)
    assert case.tickets[0]["status"] == "done"
    again = await tk.pull_ticket_status(db, case, adapter, SYNC, now=NOW)
    assert not again.retest_requested
    off = _case()
    await tk.ensure_ticket(db, off, adapter, SYNC, integration="jira", now=NOW)
    adapter.tickets["ITS-2"] = tk.RemoteTicket(key="ITS-2", url="u", status="done")
    quiet = await tk.pull_ticket_status(db, off, adapter, TicketingSync(done_starts_retest=False), now=NOW)
    assert not quiet.retest_requested


@pytest.mark.asyncio
async def test_resolved_case_closes_the_ticket_and_a_failed_retest_reopens_it():
    db, adapter, case = FakeSession(), FakeAdapter(), _case()
    await tk.ensure_ticket(db, case, adapter, SYNC, integration="jira", now=NOW)
    case.state = "resolved"
    await tk.push_case_state(db, case, adapter, SYNC, now=NOW)
    assert adapter.tickets["ITS-1"].status == "done"
    case.state = "new"
    await tk.push_case_state(db, case, adapter, SYNC, now=NOW)
    assert adapter.tickets["ITS-1"].status == "open"
    assert case.tickets[-1]["status"] == "open"


@pytest.mark.asyncio
async def test_reopen_can_open_a_linked_ticket_instead():
    db, adapter, case = FakeSession(), FakeAdapter(), _case()
    await tk.ensure_ticket(db, case, adapter, SYNC, integration="jira", now=NOW)
    case.state = "resolved"
    await tk.push_case_state(db, case, adapter, SYNC, now=NOW)
    case.state = "new"
    outcome = await tk.push_case_state(db, case, adapter, TicketingSync(on_reopen="linked"), now=NOW)
    assert outcome.created and adapter.created == 2
    assert [t["key"] for t in case.tickets] == ["ITS-1", "ITS-2"]
    assert adapter.comments and adapter.comments[0][0] == "ITS-2"


@pytest.mark.asyncio
async def test_cut_integration_gives_out_of_date_with_its_since_then_error_with_the_fix():
    db, adapter, case = FakeSession(), FakeAdapter(), _case()
    await tk.ensure_ticket(db, case, adapter, SYNC, integration="jira", now=NOW)
    adapter.fail = "401 unauthorized"
    await tk.pull_ticket_status(db, case, adapter, SYNC, now=NOW)
    later = NOW + timedelta(hours=2)
    await tk.pull_ticket_status(db, case, adapter, SYNC, now=later)
    sync = case.tickets[0]["sync"]
    assert sync["state"] == "out_of_date"
    assert sync["since"] == NOW.isoformat()
    assert sync["fix"] == "reconnect"
    assert sync["fix_code"] == "reconnect_jira"
    case.state = "resolved"
    await tk.push_case_state(db, case, adapter, SYNC, now=later)
    assert case.tickets[0]["sync"]["state"] == "error"
    assert "401" in case.tickets[0]["sync"]["error"]
    assert case.tickets[0]["sync"]["fix_code"] == "reconnect_jira"
    adapter.fail = None
    await tk.pull_ticket_status(db, case, adapter, SYNC, now=later)
    assert case.tickets[0]["sync"]["state"] == "in_sync"


@pytest.mark.asyncio
async def test_failed_create_writes_nothing_on_the_case():
    db, adapter, case = FakeSession(), FakeAdapter(), _case()
    adapter.fail = "project ITS not found"
    outcome = await tk.ensure_ticket(db, case, adapter, SYNC, integration="jira", now=NOW)
    assert outcome.error and case.tickets == []


@pytest.mark.asyncio
async def test_no_ticketing_declared_is_a_reason_not_an_error():
    profile = type("P", (), {"ticketing": TicketingTarget()})()
    assert await tk.adapter_for(FakeSession(), profile) == tk.NOT_CONNECTED


class _SyncSession(FakeSession):
    def __init__(self, cases: list[CtemCase]) -> None:
        super().__init__()
        self.cases = cases

    async def execute(self, _statement):
        cases = self.cases
        return type("R", (), {"scalars": lambda self: type("S", (), {"all": lambda s: list(cases)})()})()


@pytest.mark.asyncio
async def test_scheduled_pass_never_creates_then_pulls_pushes_and_raises_the_retest_once(monkeypatch):
    from app.services import ctem_case_loop as case_loop
    from app.services import organization_context as oc

    org = uuid.uuid4()
    mobilized = _case(organization_id=org, severity="P1")
    waiting = _case(organization_id=org, severity="P1", state="new")
    low = _case(organization_id=org, severity="P3")
    adapter = FakeAdapter()
    row = type("V", (), {"organization_id": org, "profile": {"ticketing": {"integration": "i", "project": "ITS"}}})()

    async def _current(db, organization_id=None):
        return row

    async def _adapter(db, profile):
        return adapter

    monkeypatch.setattr(oc, "current_version", _current)
    monkeypatch.setattr(tk, "adapter_for", _adapter)
    db = _SyncSession([mobilized, waiting, low])
    events = case_loop.PendingEvents()
    # The pass opens no ticket: only the Mobilization role's gated call does (gate ctem.mobilize).
    counts = await tk.sync_cases(db, events=events, now=NOW)
    assert counts["created"] == 0 and adapter.created == 0
    assert (await tk.sync_case(db, mobilized, adapter, SYNC, integration="i", now=NOW)).notes == [
        "the settings ask a ticket once the case passes Mobilize"
    ]
    created = await tk.sync_case(db, mobilized, adapter, SYNC, integration="i", create=True, now=NOW)
    assert created.action == "created" and adapter.created == 1
    assert waiting.tickets == [] and low.tickets == []
    adapter.tickets["ITS-1"] = tk.RemoteTicket(key="ITS-1", url="u", status="done", external_status="Done")
    counts = await tk.sync_cases(db, events=events, now=NOW)
    assert counts["retest"] == 1
    assert [name for name, _data, _id in events.items] == [tk.CASE_RETEST_REQUESTED]
    assert events.items[0][1]["ticket_key"] == "ITS-1"
    counts = await tk.sync_cases(db, events=events, now=NOW)
    assert counts["retest"] == 0 and len(events.items) == 1
    mobilized.state = "resolved"
    counts = await tk.sync_cases(db, events=events, now=NOW)
    assert counts["pushed"] == 0  # already done: nothing to push
    # The re-test failed after the ticket was done: the case reopens, so does the ticket.
    mobilized.state = "new"
    mobilized.validations = [{"result": "failed", "at": (NOW + timedelta(minutes=5)).isoformat()}]
    later = NOW + timedelta(minutes=10)
    counts = await tk.sync_cases(db, events=events, now=later)
    assert counts["pushed"] == 1 and adapter.tickets["ITS-1"].status == "open"


def test_a_done_ticket_waiting_for_its_retest_is_not_reopened():
    case = _case(state="remediating")
    done = {"key": "ITS-1", "status": "done", "at": NOW.isoformat(), "sync": {"state": "in_sync"}}
    assert not tk.needs_reopen(case, done)
    closed_by_case = {**done, "sync": {"state": "in_sync", "closed_by_case": True}}
    assert tk.needs_reopen(case, closed_by_case)
    old_failure = _case(validations=[{"result": "failed", "at": (NOW - timedelta(hours=1)).isoformat()}])
    assert not tk.needs_reopen(old_failure, done)


@pytest.mark.asyncio
async def test_scheduled_pass_with_no_ticketing_does_nothing(monkeypatch):
    from app.services import organization_context as oc

    async def _none(db, organization_id=None):
        return None

    monkeypatch.setattr(oc, "current_version", _none)
    counts = await tk.sync_cases(_SyncSession([_case()]), now=NOW)
    assert counts == {"created": 0, "pulled": 0, "pushed": 0, "retest": 0, "errors": 0, "not_connected": 1}


@pytest.mark.asyncio
async def test_the_mobilization_tool_path_opens_one_ticket_on_create_and_needs_ticketing(monkeypatch):
    """#917 slice 4: sync_one is the only way the Mobilization role touches a ticket."""
    from app.services import organization_context as oc

    org = uuid.uuid4()
    case = _case(organization_id=org, severity="P3", state="new")
    adapter = FakeAdapter()
    row = type("V", (), {"organization_id": org, "profile": {"ticketing": {"integration": "i", "project": "ITS"}}})()

    async def _current(db, organization_id=None):
        return row

    async def _adapter(db, profile):
        return adapter

    monkeypatch.setattr(oc, "current_version", _current)
    monkeypatch.setattr(tk, "adapter_for", _adapter)
    # Without create, a P3 case not yet mobilized gets no ticket.
    answer = await tk.sync_one(FakeSession(), case, now=NOW)
    assert answer["connected"] and answer["ticket"] is None and adapter.created == 0
    # The Mobilize decision: create opens it once, a retry finds it again.
    answer = await tk.sync_one(FakeSession(), case, create=True, now=NOW)
    assert answer["action"] == "created" and answer["ticket"]["key"] == "ITS-1"
    again = await tk.sync_one(FakeSession(), case, create=True, now=NOW)
    assert again["action"] == "pulled" and adapter.created == 1
    # A resolved case is never given a new ticket by create.
    closed = _case(organization_id=org, state="resolved")
    assert (await tk.sync_one(FakeSession(), closed, create=True, now=NOW))["ticket"] is None


@pytest.mark.asyncio
async def test_the_mobilization_tool_path_without_ticketing_is_a_reason(monkeypatch):
    from app.services import organization_context as oc

    async def _none(db, organization_id=None):
        return None

    monkeypatch.setattr(oc, "current_version", _none)
    answer = await tk.sync_one(FakeSession(), _case(), create=True, now=NOW)
    assert answer == {"connected": False, "reason": tk.NOT_CONNECTED, "settings": tk.SETTINGS_PATH}


def test_the_ticket_creation_is_never_paused_by_the_mobilize_gate() -> None:
    """#1675: the Mobilize decision is one work item; sync_ctem_case_ticket always runs."""
    from app.agents.builtin_fleet_docs import BUILTIN_FLEET_STRUCTURED
    from app.services.fleet_autonomy import AUTONOMY_LEVELS, gated_tool_rules

    gates = BUILTIN_FLEET_STRUCTURED["ctem-vuln-prioritization"]["gates"]
    for level in AUTONOMY_LEVELS:
        assert not [r for r in gated_tool_rules(level, gates) if r["name"] == "sync_ctem_case_ticket"]


@pytest.mark.asyncio
async def test_a_jira_status_event_moves_its_case_at_once_and_asks_the_retest_once(monkeypatch):
    from app.services import ctem_case_loop as case_loop
    from app.services import organization_context as oc

    org = uuid.uuid4()
    case = _case(organization_id=org)
    other = _case(organization_id=org)
    adapter = FakeAdapter()
    await tk.ensure_ticket(FakeSession(), case, adapter, SYNC, integration="jira-1", now=NOW)
    row = type(
        "V", (), {"organization_id": org, "profile": {"ticketing": {"integration": "jira-1", "project": "ITS"}}}
    )()

    async def _current(db, organization_id=None):
        return row

    monkeypatch.setattr(oc, "current_version", _current)
    db = _SyncSession([other, case])
    events = case_loop.PendingEvents()
    moved = await tk.apply_inbound_status(
        db, integration_id="jira-1", key="ITS-1", status_name="In Progress", category="indeterminate", events=events
    )
    assert moved is not None and moved.updated and not moved.retest_requested
    assert tk.managed_ticket(case)["status"] == "in_progress" and other.tickets == []
    done = await tk.apply_inbound_status(
        db, integration_id="jira-1", key="ITS-1", status_name="Done", category="done", events=events
    )
    assert done.retest_requested and case.state == "validating"  # the case closes on the re-test, never on the ticket
    again = await tk.apply_inbound_status(
        db, integration_id="jira-1", key="ITS-1", status_name="Done", category="done", events=events
    )
    assert not again.updated and [n for n, _d, _i in events.items] == [tk.CASE_RETEST_REQUESTED]
    assert tk.managed_ticket(case)["sync"]["state"] == "in_sync"
    # CTEM-19-UI (#1032): each move raised the live frame, the unchanged replay none,
    # and the case history says the re-test started because the ticket is done.
    assert [f["event_kind"] for f in events.frames] == ["ticket", "ticket"]
    assert all(f["case_id"] == str(case.id) for f in events.frames)
    retests = [r for r in db.added if getattr(r, "kind", None) == "retest_requested"]
    assert len(retests) == 1
    assert getattr(retests[0], "evidence", None) == "jira-1:ITS-1" and getattr(retests[0], "actor", None) == "system"


@pytest.mark.asyncio
async def test_the_case_screens_read_whether_ticketing_is_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    """CTEM-19-UI (#1032): the profile alone answers, no credential and no remote call."""
    from app.services import organization_context as oc

    profile: dict[str, Any] = {"ticketing": {"integration": "", "project": ""}}
    row = type("V", (), {"organization_id": uuid.uuid4(), "profile": profile})()
    db = cast(AsyncSession, FakeSession())

    async def _current(db: object, organization_id: object = None) -> object:
        return row

    async def _none(db: object, organization_id: object = None) -> None:
        return None

    monkeypatch.setattr(oc, "current_version", _none)
    assert await tk.ticketing_target(db) == {
        "connected": False,
        "reason": tk.NOT_CONNECTED,
        "settings": tk.SETTINGS_PATH,
    }
    monkeypatch.setattr(oc, "current_version", _current)
    assert (await tk.ticketing_target(db))["connected"] is False
    profile["ticketing"] = {"integration": "jira-1", "project": "ITS"}
    assert await tk.ticketing_target(db) == {"connected": True, "settings": tk.SETTINGS_PATH}


@pytest.mark.asyncio
async def test_an_event_from_another_integration_or_an_unknown_key_moves_nothing(monkeypatch):
    from app.services import organization_context as oc

    org = uuid.uuid4()
    case = _case(organization_id=org)
    await tk.ensure_ticket(FakeSession(), case, FakeAdapter(), SYNC, integration="jira-1", now=NOW)
    row = type(
        "V", (), {"organization_id": org, "profile": {"ticketing": {"integration": "jira-1", "project": "ITS"}}}
    )()

    async def _current(db, organization_id=None):
        return row

    monkeypatch.setattr(oc, "current_version", _current)
    db = _SyncSession([case])
    assert await tk.apply_inbound_status(db, integration_id="jira-2", key="ITS-1", status_name="Done") is None
    assert await tk.apply_inbound_status(db, integration_id="jira-1", key="ITS-9", status_name="Done") is None
    assert tk.managed_ticket(case)["status"] == "open"


@pytest.mark.asyncio
async def test_a_jira_delivery_without_a_status_or_with_a_failure_never_raises(monkeypatch):
    assert await tk.apply_jira_delivery("jira-1", {"issue": {"key": "ITS-1", "fields": {}}}) is None
    assert await tk.apply_jira_delivery("jira-1", {"no": "issue"}) is None

    async def _boom(db, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(tk, "apply_inbound_status", _boom)
    delivery = {"issue": {"key": "ITS-1", "fields": {"status": {"name": "Done", "statusCategory": {"key": "done"}}}}}
    assert await tk.apply_jira_delivery("jira-1", delivery) is None


def test_retest_event_is_declared_platform_wide():
    from app.services import platform_events

    assert tk.CASE_RETEST_REQUESTED in platform_events.PLATFORM_EVENTS
    # A case event: heard only by readers of the case (#933), never platform-wide.
    assert tk.CASE_RETEST_REQUESTED in platform_events.VIEWER_CHECKED_EVENTS
    assert tk.CASE_RETEST_REQUESTED not in platform_events.PLATFORM_WIDE_EVENTS


def test_sync_settings_default_and_parse_from_a_saved_profile():
    target = TicketingTarget.model_validate({"integration": "abc", "project": "ITS"})
    assert target.sync.create_on == "mobilize_p1_p2"
    assert target.sync.done_starts_retest is True
    with pytest.raises(ValueError):
        TicketingTarget.model_validate({"integration": "abc", "project": "ITS", "sync": {"create_on": "always"}})


def test_every_fix_has_a_stable_code_the_ui_words() -> None:
    # The UI words the fix from its code (case-ticket.tsx FIX_LABELS), never from the English sentence.
    assert tk.TicketingError("x").fix_code == "check_integration"
    assert tk.TicketingError("x", fix="y", fix_code="map_jira_status").fix_code == "map_jira_status"
    assert tk.TicketingError("x", fix_code="unknown").fix_code == "check_integration"
    assert set(tk.FIX_CODES) == {
        "check_integration",
        "reconnect_jira",
        "map_jira_status",
        "choose_servicenow_table",
        "check_servicenow_credentials",
        "relink_case",
    }
