"""#2390: a decision gate never waits silently for an account nobody uses.

A ``needs_human`` item addressed to the seed account, or to a person who never
signs in, was valid and waited for days with nobody told. Three signals, none
refusing a write: a warning on the tool answer when the recipient is unlikely
to see the gate, a one-time administrators' alert and
``work_item.needs_human_unseen`` for a gate nobody opened in time, and the
fleet-health findings ``decision_unseen`` and ``decision_recipient_inactive``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import fleet_health_signals as signals
from app.services import platform_events, work_item_decision_reach
from app.services.work_item_decision_reach import (
    OPENED_MARK_KEY,
    UNSEEN_EVENT,
    UNSEEN_MARK_KEY,
    carry_decision_marks,
    clear_decision_marks,
    group_inactive_reason,
    is_unseen,
    person_inactive_reason,
    warning_line,
)

_NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
_SEED = "admin@filigran.io"


# -- The recipient at the write -------------------------------------------------------


def test_a_recipient_with_no_login_row_is_named_in_a_warning():
    reason = person_inactive_reason(email="p@example.com", last_login=None, now=_NOW, days=7, seed_email=_SEED)
    assert reason == "the recipient has never signed in"
    assert "may wait unseen" in warning_line(reason) and "recipient" in warning_line(reason)


def test_a_recipient_signed_in_yesterday_gets_no_warning():
    yesterday = _NOW - timedelta(days=1)
    assert (
        person_inactive_reason(email="p@example.com", last_login=yesterday, now=_NOW, days=7, seed_email=_SEED) is None
    )


def test_a_recipient_quiet_past_the_window_is_named_with_the_date():
    old = _NOW - timedelta(days=12)
    reason = person_inactive_reason(email="p@example.com", last_login=old, now=_NOW, days=7, seed_email=_SEED)
    assert reason is not None and old.date().isoformat() in reason and "7 days" in reason


def test_the_seed_account_is_flagged_even_when_it_signed_in():
    reason = person_inactive_reason(email="Admin@Filigran.io", last_login=_NOW, now=_NOW, days=7, seed_email=_SEED)
    assert reason is not None and "seed account" in reason


def test_a_group_warns_only_when_no_member_signed_in_within_the_window():
    assert group_inactive_reason([None, _NOW - timedelta(days=2)], now=_NOW, days=7) is None
    assert "no member" in (group_inactive_reason([None, _NOW - timedelta(days=30)], now=_NOW, days=7) or "")
    assert "no member" in (group_inactive_reason([], now=_NOW, days=7) or "")


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _BatchDb:
    """Answers the three reads of ``inactive_reasons``: memberships, emails, logins."""

    def __init__(self, *, members=(), emails=(), logins=()):
        self._members, self._emails, self._logins = list(members), list(emails), list(logins)

    async def execute(self, statement):
        sql = str(statement)
        if "group_members" in sql:
            return _Result(self._members)
        if "audit_log" in sql:
            return _Result(self._logins)
        return _Result(self._emails)


@pytest.mark.asyncio
async def test_inactive_reasons_reads_people_groups_and_the_administrators_in_one_batch():
    fresh, gone, group_member, group = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db = _BatchDb(
        members=[(group, group_member)],
        emails=[(fresh, "a@example.com", True), (gone, "b@example.com", True)],
        logins=[(fresh, _NOW - timedelta(hours=3))],
    )
    reasons = await work_item_decision_reach.inactive_reasons(
        db, [(fresh, None), (gone, None), (None, [str(group)]), (None, None), (uuid.uuid4(), None)], now=_NOW
    )
    assert reasons[0] is None
    assert reasons[1] == "the recipient has never signed in"
    assert reasons[2] is not None and "no member" in reasons[2]
    assert reasons[3] is None  # the administrators are never inactive
    assert reasons[4] == "the recipient is no longer on the platform"


@pytest.mark.asyncio
async def test_the_tool_line_carries_the_warning(monkeypatch):
    from app.core import database
    from app.services import work_item_recipient

    person = SimpleNamespace(id=uuid.uuid4(), full_name="Pat", email="p@example.com")

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    async def _user(_db, _ref):
        return person

    async def _reason(_db, *, user_id, group_ids, now=None):
        return "the recipient has never signed in"

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    monkeypatch.setattr(work_item_recipient, "_user_by_ref", _user)
    monkeypatch.setattr(work_item_decision_reach, "inactive_reason", _reason)
    line = await work_item_recipient.recipient_line({"needs_human": True, "waits_for_user_id": str(person.id)})
    assert line is not None
    assert line.startswith("  - waits for: Pat (p@example.com)")
    assert "warning: the recipient has never signed in" in line


# -- The marks ------------------------------------------------------------------------


def test_a_writer_rewriting_the_payload_keeps_the_decision_marks():
    stored = {"case": "1", OPENED_MARK_KEY: "t1", UNSEEN_MARK_KEY: "t2"}
    assert carry_decision_marks({"case": "2"}, stored) == {"case": "2", OPENED_MARK_KEY: "t1", UNSEEN_MARK_KEY: "t2"}
    assert carry_decision_marks({"case": "2"}, {"case": "1"}) == {"case": "2"}
    assert carry_decision_marks(None, stored) is None


def test_a_new_gate_clears_the_marks():
    item = SimpleNamespace(payload={"case": "1", OPENED_MARK_KEY: "t1", UNSEEN_MARK_KEY: "t2"})
    clear_decision_marks(item)
    assert item.payload == {"case": "1"}


def test_unseen_needs_the_threshold_and_no_opening():
    old = _NOW - timedelta(hours=30)
    assert is_unseen({}, waiting_since=old, now=_NOW, hours=24)
    assert not is_unseen({}, waiting_since=_NOW - timedelta(hours=2), now=_NOW, hours=24)
    assert not is_unseen({OPENED_MARK_KEY: "t"}, waiting_since=old, now=_NOW, hours=24)


# -- The unseen alert -----------------------------------------------------------------


def test_the_unseen_event_is_in_the_catalog():
    spec = platform_events.PLATFORM_EVENTS[UNSEEN_EVENT]
    for key in ("owner_agent_id", "correlation_key", "unseen_hours"):
        assert key in spec["filter_keys"], key


def _gate(**overrides):
    values = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "owner_agent_id": uuid.uuid4(),
        "created_by_agent_id": None,
        "claimed_by_agent_id": None,
        "claimed_by_run_id": None,
        "correlation_key": "ctem:case:7",
        "title": "Accept the risk?",
        "status": "blocked",
        "priority": "high",
        "payload": {},
        "is_company_managed": False,
        "workspace_id": None,
        "blocked_reason": None,
        "blocked_on": None,
        "blocked_on_refs": None,
        "needs_human": True,
        "waits_for_user_id": uuid.uuid4(),
        "waits_for_group_ids": None,
        "last_activity_at": _NOW - timedelta(hours=30),
        "due_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _UnseenSession:
    """First execute: the gate ids; then per id the locked re-read (recipient reads are patched).

    Then the administrators' phase locks again each gate it marks, in sweep order.
    """

    def __init__(self, gates, rereads=None):
        rereads = gates if rereads is None else rereads
        self._results = [_Rows([g.id for g in gates])]
        for r in rereads:
            self._results.append(_Rows([r] if r else []))
        for r in rereads:
            if r:
                self._results.append(_Rows([r]))
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _statement):
        return self._results.pop(0)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _patch(monkeypatch, session, published, notified, *, admins=(), reasons=None, members=None):
    """*reasons*: recipient id (person or first group) to inactive reason; *members*: group id to member ids."""
    from app.core import database
    from app.services import notify

    reasons, members = reasons or {}, members or {}

    async def _publish(name, data, *, event_id=None):
        published.append((name, data, event_id, session.commits))
        return event_id

    async def _notify(_db, user_id, **kwargs):
        notified.append((user_id, kwargs))

    async def _reason(_db, *, user_id, group_ids, now=None):
        key = str(group_ids[0]) if group_ids else str(user_id)
        return reasons.get(key)

    async def _admin_ids(_db):
        return [str(a) for a in admins]

    async def _members(_db, group_ids):
        return [str(u) for g in group_ids for u in members.get(str(g), [])]

    monkeypatch.setattr(database, "async_session", lambda: session)
    monkeypatch.setattr(platform_events, "publish", _publish)
    monkeypatch.setattr(notify, "notify_web_user", _notify)
    monkeypatch.setattr(work_item_decision_reach, "inactive_reason", _reason)
    monkeypatch.setattr(work_item_decision_reach, "_admin_ids", _admin_ids)
    monkeypatch.setattr(work_item_decision_reach, "_group_member_ids", _members)


def test_the_administrators_are_the_fallback_only():
    person, group = uuid.uuid4(), uuid.uuid4()
    assert work_item_decision_reach.reminder_targets(person=person, group_ids=None, reason=None) == ("person", person)
    assert work_item_decision_reach.reminder_targets(person=person, group_ids=[group], reason=None) == (
        "groups",
        [group],
    )
    assert work_item_decision_reach.reminder_targets(person=person, group_ids=None, reason="gone") == ("admins", "gone")
    assert work_item_decision_reach.reminder_targets(person=None, group_ids=None, reason=None) == (
        "admins",
        work_item_decision_reach.ADDRESSED_TO_ADMINS,
    )


def test_a_deactivated_or_removed_recipient_has_no_real_recipient():
    reason = person_inactive_reason(
        email="p@example.com", last_login=_NOW, now=_NOW, days=7, seed_email=_SEED, active=False
    )
    assert reason == "the recipient's account is deactivated"


@pytest.mark.asyncio
async def test_an_active_recipient_is_reminded_and_no_administrator_is_told(monkeypatch):
    gate = _gate()
    session = _UnseenSession([gate])
    published, notified = [], []
    _patch(monkeypatch, session, published, notified, admins=[uuid.uuid4(), uuid.uuid4()])

    assert await work_item_decision_reach.raise_unseen_decisions(now=_NOW) == 1

    assert [u for u, _ in notified] == [str(gate.waits_for_user_id)]
    assert notified[0][1]["extra_data"]["work_item_id"] == str(gate.id)
    (name, data, event_id, commits_before) = published[0]
    assert name == UNSEEN_EVENT and commits_before == 1
    assert event_id == work_item_decision_reach.unseen_event_id(gate)
    assert data["work_item_id"] == str(gate.id) and data["unseen_hours"] == 24
    assert data["notified_admin_ids"] == [] and data["reminded_user_ids"] == [str(gate.waits_for_user_id)]
    assert data["no_recipient_reason"] is None
    assert gate.payload[UNSEEN_MARK_KEY] == _NOW.isoformat()


@pytest.mark.asyncio
async def test_a_group_recipient_reminds_its_active_members(monkeypatch):
    group, member = uuid.uuid4(), uuid.uuid4()
    gate = _gate(waits_for_user_id=None, waits_for_group_ids=[str(group)])
    session = _UnseenSession([gate])
    published, notified = [], []
    _patch(monkeypatch, session, published, notified, admins=[uuid.uuid4()], members={str(group): [member]})

    assert await work_item_decision_reach.raise_unseen_decisions(now=_NOW) == 1
    assert [u for u, _ in notified] == [str(member)]


@pytest.mark.parametrize(
    "reason",
    [
        "the recipient has never signed in",
        "the recipient is the platform's technical seed account, which no person signs in to",
        "no member of its recipient groups signed in within 7 days",
    ],
)
@pytest.mark.asyncio
async def test_a_gate_nobody_can_answer_goes_to_the_administrators(monkeypatch, reason):
    gate = _gate()
    admins = [uuid.uuid4(), uuid.uuid4()]
    session = _UnseenSession([gate])
    published, notified = [], []
    _patch(monkeypatch, session, published, notified, admins=admins, reasons={str(gate.waits_for_user_id): reason})

    assert await work_item_decision_reach.raise_unseen_decisions(now=_NOW) == 1

    assert sorted(u for u, _ in notified) == sorted(str(a) for a in admins)
    assert all(kw["extra_data"]["work_item_ids"] == [str(gate.id)] for _, kw in notified)
    assert all(reason in kw["summary"] for _, kw in notified)
    data = published[0][1]
    assert sorted(data["notified_admin_ids"]) == sorted(str(a) for a in admins)
    assert data["no_recipient_reason"] == reason and data["reminded_user_ids"] == []


@pytest.mark.asyncio
async def test_five_gates_nobody_can_answer_make_one_summary_per_administrator(monkeypatch):
    gates = [_gate(title=f"Decision {n}") for n in range(5)]
    admins = [uuid.uuid4(), uuid.uuid4()]
    session = _UnseenSession(gates)
    published, notified = [], []
    reasons = {str(g.waits_for_user_id): "the recipient has never signed in" for g in gates}
    _patch(monkeypatch, session, published, notified, admins=admins, reasons=reasons)

    assert await work_item_decision_reach.raise_unseen_decisions(now=_NOW) == 5

    assert sorted(u for u, _ in notified) == sorted(str(a) for a in admins)
    for _, kw in notified:
        assert kw["title"].startswith("5 decisions have waited 24 hours")
        assert kw["extra_data"]["work_item_ids"] == [str(g.id) for g in gates]
        assert all(f"Decision {n}" in kw["summary"] for n in range(5))
    assert len(published) == 5


@pytest.mark.asyncio
async def test_a_group_with_no_active_member_falls_back_to_the_administrators(monkeypatch):
    group = uuid.uuid4()
    gate = _gate(waits_for_user_id=None, waits_for_group_ids=[str(group)])
    admins = [uuid.uuid4()]
    session = _UnseenSession([gate])
    published, notified = [], []
    _patch(monkeypatch, session, published, notified, admins=admins, members={str(group): []})

    assert await work_item_decision_reach.raise_unseen_decisions(now=_NOW) == 1

    assert [u for u, _ in notified] == [str(admins[0])]
    assert work_item_decision_reach.NO_ACTIVE_GROUP_MEMBER in notified[0][1]["summary"]
    data = published[0][1]
    assert data["no_recipient_reason"] == work_item_decision_reach.NO_ACTIVE_GROUP_MEMBER
    assert data["notified_admin_ids"] == [str(admins[0])] and data["reminded_user_ids"] == []
    assert gate.payload[UNSEEN_MARK_KEY] == _NOW.isoformat()


@pytest.mark.asyncio
async def test_a_failed_administrator_summary_leaves_the_gate_unmarked_for_the_next_tick(monkeypatch):
    from app.services import notify

    gate = _gate()
    session = _UnseenSession([gate])
    published, notified = [], []
    reasons = {str(gate.waits_for_user_id): "the recipient has never signed in"}
    _patch(monkeypatch, session, published, notified, admins=[uuid.uuid4()], reasons=reasons)

    async def _broken(_db, _user_id, **_kwargs):
        raise RuntimeError("bell down")

    monkeypatch.setattr(notify, "notify_web_user", _broken)
    committed_marks = []
    real_commit = session.commit

    async def _commit():
        committed_marks.append(UNSEEN_MARK_KEY in (gate.payload or {}))
        await real_commit()

    session.commit = _commit

    assert await work_item_decision_reach.raise_unseen_decisions(now=_NOW) == 0

    # The only commit is the per-gate one, before any mark: the rolled-back
    # summary transaction held the mark, so the next tick takes the gate again.
    assert committed_marks == [False]
    assert published == [] and session.rollbacks == 1


class _ExpiringGate(SimpleNamespace):
    """A gate whose attributes cannot be read once a rollback expired it, as under AsyncSession."""

    expired = False

    def __getattribute__(self, name):
        if name not in ("expired", "__dict__", "__class__") and object.__getattribute__(self, "expired"):
            raise RuntimeError(f"MissingGreenlet: expired attribute {name} read after a rollback")
        return object.__getattribute__(self, name)


class _ExpiringSession(_UnseenSession):
    """A rollback expires every gate of the session; a locked re-read refreshes the row it returns."""

    def __init__(self, gates, rereads=None):
        super().__init__(gates, rereads)
        self._gates = list(gates)

    async def execute(self, statement):
        rows = await super().execute(statement)
        for row in rows._rows:
            if isinstance(row, _ExpiringGate):
                row.expired = False
        return rows

    async def rollback(self):
        await super().rollback()
        for gate in self._gates:
            gate.expired = True


@pytest.mark.asyncio
async def test_a_failed_administrator_summary_reads_no_expired_gate(monkeypatch):
    from app.services import notify

    gate = _ExpiringGate(**vars(_gate()))
    session = _ExpiringSession([gate])
    published, notified = [], []
    reasons = {str(gate.waits_for_user_id): "the recipient has never signed in"}
    _patch(monkeypatch, session, published, notified, admins=[uuid.uuid4()], reasons=reasons)

    async def _broken(_db, _user_id, **_kwargs):
        raise RuntimeError("bell down")

    monkeypatch.setattr(notify, "notify_web_user", _broken)

    assert await work_item_decision_reach.raise_unseen_decisions(now=_NOW) == 0
    assert published == [] and session.rollbacks == 1


@pytest.mark.asyncio
async def test_a_rollback_in_the_sweep_does_not_break_the_administrator_summary(monkeypatch):
    """A gate queued for the administrators, then another tick holding the next gate (rollback)."""
    queued = _ExpiringGate(**vars(_gate(title="Nobody can answer")))
    held = _ExpiringGate(**vars(_gate()))
    admins = [uuid.uuid4()]
    session = _ExpiringSession([queued, held], rereads=[queued, None])
    published, notified = [], []
    reasons = {str(queued.waits_for_user_id): "the recipient has never signed in"}
    _patch(monkeypatch, session, published, notified, admins=admins, reasons=reasons)

    assert await work_item_decision_reach.raise_unseen_decisions(now=_NOW) == 1
    assert [u for u, _ in notified] == [str(admins[0])]
    assert notified[0][1]["extra_data"]["work_item_ids"] == [str(queued.id)]
    assert published[0][1]["notified_admin_ids"] == [str(admins[0])]


@pytest.mark.asyncio
async def test_a_sweep_of_active_recipients_tells_no_administrator(monkeypatch):
    """The 25 Nov 2026 backlog: eleven gates addressed to active people, zero administrator notices."""
    gates = [_gate(title=f"Decision {n}") for n in range(11)]
    admins = [uuid.uuid4()]
    session = _UnseenSession(gates)
    published, notified = [], []
    _patch(monkeypatch, session, published, notified, admins=admins)

    assert await work_item_decision_reach.raise_unseen_decisions(now=_NOW) == 11
    assert str(admins[0]) not in {u for u, _ in notified}
    assert sorted(u for u, _ in notified) == sorted(str(g.waits_for_user_id) for g in gates)


@pytest.mark.asyncio
async def test_a_second_sweep_sends_nothing_new(monkeypatch):
    """An alerted gate (the old rule's included) carries the mark, so the query no longer returns it."""
    session = _UnseenSession([])
    published, notified = [], []
    _patch(monkeypatch, session, published, notified, admins=[uuid.uuid4()])

    assert await work_item_decision_reach.raise_unseen_decisions(now=_NOW) == 0
    assert published == [] and notified == []


@pytest.mark.asyncio
async def test_a_gate_taken_by_another_tick_raises_nothing(monkeypatch):
    gate = _gate()
    session = _UnseenSession([gate], rereads=[None])
    published, notified = [], []
    _patch(monkeypatch, session, published, notified)

    assert await work_item_decision_reach.raise_unseen_decisions(now=_NOW) == 0
    assert published == [] and notified == [] and session.rollbacks == 1


# -- Opening the item ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opening_a_gate_stamps_it_once(monkeypatch):
    from app.core import database

    gate = _gate()

    class _OpenSession(_UnseenSession):
        def __init__(self, item):
            self._results = [_Rows([item]) if item else _Rows([])]
            self.commits = 0
            self.rollbacks = 0

    session = _OpenSession(gate)
    monkeypatch.setattr(database, "async_session", lambda: session)
    assert await work_item_decision_reach.mark_opened(str(gate.id), now=_NOW) is True
    assert gate.payload[OPENED_MARK_KEY] == _NOW.isoformat() and session.commits == 1

    again = _OpenSession(gate)
    monkeypatch.setattr(database, "async_session", lambda: again)
    assert await work_item_decision_reach.mark_opened(str(gate.id), now=_NOW) is False
    assert again.commits == 0
    assert await work_item_decision_reach.mark_opened("not-an-id") is False


# -- Fleet health ----------------------------------------------------------------------


def test_the_two_findings_are_owner_fixes_with_advice():
    for kind in ("decision_unseen", "decision_recipient_inactive"):
        assert signals.FINDING_FIX[kind] == signals.FIX_OWNER
        assert "recipient" in signals.FINDING_ADVICE[kind]


class _FindingsDb:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _statement):
        return _Result(self.rows)


@pytest.mark.asyncio
async def test_fleet_health_reports_unseen_and_inactive_gates(monkeypatch):
    unseen_id, opened_id, fresh_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    quiet, active = uuid.uuid4(), uuid.uuid4()
    rows = [
        (unseen_id, {}, _NOW - timedelta(hours=30), quiet, None, None),
        (opened_id, {OPENED_MARK_KEY: "t"}, _NOW - timedelta(hours=30), active, None, None),
        (fresh_id, {}, _NOW - timedelta(hours=1), active, None, None),
    ]

    async def _reasons(_db, recipients, *, now=None):
        return ["the recipient has never signed in" if person == quiet else None for person, _ in recipients]

    monkeypatch.setattr(work_item_decision_reach, "inactive_reasons", _reasons)
    findings = await signals._decision_reach_findings(_FindingsDb(rows), True, _NOW)
    by_kind = {f.kind: f for f in findings}
    assert set(by_kind) == {"decision_unseen", "decision_recipient_inactive"}
    assert by_kind["decision_unseen"].evidence == [str(unseen_id)]
    assert by_kind["decision_recipient_inactive"].evidence == [str(unseen_id)]


@pytest.mark.asyncio
async def test_fleet_health_says_nothing_when_no_gate_is_open():
    assert await signals._decision_reach_findings(_FindingsDb([]), True, _NOW) == []
