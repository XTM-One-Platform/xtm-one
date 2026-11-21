"""Two-way ticketing sync for CTEM cases (#917): contract, idempotency, status mapping.

``services/ctem_ticketing.py`` with a fake adapter and a fake session: one
ticket per case whatever the replays, the status mapping both ways, the sync
states (in sync, out of date with its since, error with its fix) and the
re-test asked when a ticket reaches done.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

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
            raise tk.TicketingError(self.fail, fix="reconnect")

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
        state="in_progress",
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
    assert case.state == "in_progress"
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
    case.state = "needs_attention"
    await tk.push_case_state(db, case, adapter, SYNC, now=NOW)
    assert adapter.tickets["ITS-1"].status == "open"
    assert case.tickets[-1]["status"] == "open"


@pytest.mark.asyncio
async def test_reopen_can_open_a_linked_ticket_instead():
    db, adapter, case = FakeSession(), FakeAdapter(), _case()
    await tk.ensure_ticket(db, case, adapter, SYNC, integration="jira", now=NOW)
    case.state = "resolved"
    await tk.push_case_state(db, case, adapter, SYNC, now=NOW)
    case.state = "needs_attention"
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
    case.state = "resolved"
    await tk.push_case_state(db, case, adapter, SYNC, now=later)
    assert case.tickets[0]["sync"]["state"] == "error"
    assert "401" in case.tickets[0]["sync"]["error"]
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
async def test_scheduled_pass_creates_pulls_pushes_and_raises_the_retest_once(monkeypatch):
    from app.services import ctem_case_loop as case_loop
    from app.services import organization_context as oc

    org = uuid.uuid4()
    mobilized = _case(organization_id=org, severity="P1")
    waiting = _case(organization_id=org, severity="P1", state="needs_attention")
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
    counts = await tk.sync_cases(db, events=events, now=NOW)
    assert counts["created"] == 1 and adapter.created == 1
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
    mobilized.state = "needs_attention"
    mobilized.validations = [{"result": "failed", "at": (NOW + timedelta(minutes=5)).isoformat()}]
    later = NOW + timedelta(minutes=10)
    counts = await tk.sync_cases(db, events=events, now=later)
    assert counts["pushed"] == 1 and adapter.tickets["ITS-1"].status == "open"


def test_a_done_ticket_waiting_for_its_retest_is_not_reopened():
    case = _case(state="in_progress")
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


def test_retest_event_is_declared_platform_wide():
    from app.services import platform_events

    assert tk.CASE_RETEST_REQUESTED in platform_events.PLATFORM_EVENTS
    assert tk.CASE_RETEST_REQUESTED in platform_events.PLATFORM_WIDE_EVENTS


def test_sync_settings_default_and_parse_from_a_saved_profile():
    target = TicketingTarget.model_validate({"integration": "abc", "project": "ITS"})
    assert target.sync.create_on == "mobilize_p1_p2"
    assert target.sync.done_starts_retest is True
    with pytest.raises(ValueError):
        TicketingTarget.model_validate({"integration": "abc", "project": "ITS", "sync": {"create_on": "always"}})
