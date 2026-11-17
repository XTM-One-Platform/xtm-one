"""One run per inbound event on an agent: the engine side (``services/event_run_dedup.py``).

``trigger_assignment`` and ``trigger_or_queue_assignment`` derive the run's
dedup key, record it in ``trigger_context.dedup_key`` and, under an agent-wide
advisory lock, refuse a run whose key another run of the same AGENT already
carries within the window: the refusal is a ``skipped`` run naming the
original, counted in the assignment's filtered events. Fake session, as
``test_assignment_trigger_guards.py``; the real cross-assignment query runs in
``test_event_run_dedup_postgres.py``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.models.agent import Agent
from app.models.assignment import Assignment
from app.services import assignment_engine as ae
from app.services import event_run_dedup as dedup

MAIL_CONTEXT = {
    "type": "integration_event",
    "provider": "microsoft",
    "event": "new_email",
    "message_id": "AAMk-service",
}


def _mail_payload(message_id: str = "<knox@mail.example>", **extra) -> dict:
    return {
        "from": "person@example.org",
        "subject": "Interview",
        "received_at": "2026-10-30T17:25:24Z",
        "message_id": "AAMk-service",
        "conversation_id": "AAQk-thread",
        "internet_message_id": message_id,
        "trigger_type": "integration_event",
        "event": "new_email",
        "provider": "microsoft",
        **extra,
    }


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value

    def scalar(self):
        return self._value


class _FakeSession:
    """Queued scalar results in FIFO order; advisory locks never consume one."""

    def __init__(self, results=()):
        self.results = list(results)
        self.statements = []
        self.commits = 0
        self.added = []

    async def execute(self, stmt, params=None):
        self.statements.append((stmt, params))
        if "pg_advisory_xact_lock" in str(stmt):
            return _FakeResult(None)
        if not self.results:
            return _FakeResult(None)
        return _FakeResult(self.results.pop(0))

    def lock_scopes(self) -> list[str]:
        return [
            (params or {}).get("scope", "")
            for stmt, params in self.statements
            if "pg_advisory_xact_lock" in str(stmt)
        ]

    def add(self, obj):
        self.added.append(obj)
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1


@pytest.fixture(autouse=True)
def _engine(monkeypatch):
    """AI on, no orchestration, the filtered-events counter captured."""
    created: list[dict] = []
    counted: list[tuple] = []

    async def _never_refuse(_assignment):
        return False

    async def _no_owner_check(_db, _assignment):
        return None

    async def _mock_create_execution(db, **kwargs):
        created.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    async def _mock_wake():
        return None

    async def _count(assignment_id, count):
        counted.append((assignment_id, count))

    reasons: list[tuple] = []

    async def _reasons(assignment_id, per):
        reasons.append((assignment_id, dict(per)))

    from app.services import integration_event_filters

    monkeypatch.setattr(ae, "_refuse_if_ai_disabled", _never_refuse)
    monkeypatch.setattr(ae, "refuse_if_owner_deactivated", _no_owner_check)
    monkeypatch.setattr(ae, "create_execution", _mock_create_execution)
    monkeypatch.setattr(ae, "notify_orchestration_wake", _mock_wake)
    monkeypatch.setattr(ae.settings, "scheduled_assignment_spread_seconds", 0)
    monkeypatch.setattr(ae.settings, "assignment_event_dedup_window_seconds", 600)
    monkeypatch.setattr(integration_event_filters, "record_filtered_events", _count)
    monkeypatch.setattr(integration_event_filters, "record_filter_reasons", _reasons)
    return SimpleNamespace(created=created, counted=counted, reasons=reasons)


def _agent() -> Agent:
    return Agent(id=uuid.uuid4(), name="Executive Assistant", enabled=True)


def _assignment(agent: Agent, name: str = "Inbox", trigger_config: dict | None = None) -> Assignment:
    return Assignment(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name=name,
        trigger_type="integration_event",
        trigger_config=trigger_config or {},
        prompt="Handle the mail",
        enabled=True,
    )


def _patch_duplicate(monkeypatch, original: uuid.UUID | None, seen: list | None = None):
    """What ``duplicate_event_run`` answers, and what it was asked."""

    async def _duplicate(_db, agent_id, key, *, window_seconds):
        if seen is not None:
            seen.append((agent_id, key, window_seconds))
        return original

    monkeypatch.setattr(ae, "duplicate_event_run", _duplicate)


# -- trigger_assignment -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_first_mail_run_starts_and_records_its_key(monkeypatch, _engine):
    agent = _agent()
    assignment = _assignment(agent)
    asked: list = []
    _patch_duplicate(monkeypatch, None, asked)
    session = _FakeSession()

    run_id = await ae.trigger_assignment(assignment, agent, _mail_payload(), db=session, trigger_context=dict(MAIL_CONTEXT))

    assert run_id is not None
    run = session.added[0]
    assert run.status == "pending"
    assert run.trigger_context[dedup.DEDUP_KEY_CONTEXT] == "mail:knox@mail.example"
    assert dedup.DUPLICATE_OF_CONTEXT not in run.trigger_context
    assert asked == [(agent.id, "mail:knox@mail.example", 600)]
    # The agent-wide lock, keyed by the agent, taken before the check.
    assert session.lock_scopes() == [f"agent_event_dedup:{agent.id}"]
    assert len(_engine.created) == 1
    assert _engine.counted == []


@pytest.mark.asyncio
async def test_the_same_mail_on_another_assignment_of_the_agent_is_a_skipped_run(monkeypatch, _engine):
    """The Knox scenario: one mail, two mailboxes, two assignments, one agent."""
    agent = _agent()
    second = _assignment(agent, name="Person inbox pick-up")
    original = uuid.uuid4()
    _patch_duplicate(monkeypatch, original)
    session = _FakeSession()
    # The person's mailbox: another Graph id, the same Message-ID.
    payload = {**_mail_payload(message_id="<KNOX@mail.example>"), "message_id": "AAMk-person"}

    run_id = await ae.trigger_assignment(
        second, agent, payload, db=session, trigger_context={**MAIL_CONTEXT, "message_id": "AAMk-person"}
    )

    assert run_id is not None
    run = session.added[0]
    assert str(run.id) == run_id
    assert run.status == "skipped"
    assert run.assignment_id == second.id and run.agent_id == agent.id
    assert run.result_summary.startswith(f"{dedup.DUPLICATE_SKIP_PREFIX} {original}")
    assert run.trigger_context[dedup.DEDUP_KEY_CONTEXT] == "mail:knox@mail.example"
    assert run.trigger_context[dedup.DUPLICATE_OF_CONTEXT] == str(original)
    assert run.trigger_payload == payload
    assert run.started_at is not None and run.completed_at is not None
    # Nothing executes; the refusal is committed (the locks release) and counted.
    assert _engine.created == []
    assert session.commits == 1
    assert _engine.counted == [(second.id, 1)]


@pytest.mark.asyncio
async def test_the_overlap_guard_locks_the_assignment_before_the_agent(monkeypatch, _engine):
    agent = _agent()
    assignment = _assignment(agent)
    _patch_duplicate(monkeypatch, None)

    async def _not_active(_db, _assignment_id):
        return False

    monkeypatch.setattr(ae, "has_active_run", _not_active)
    session = _FakeSession()

    await ae.trigger_assignment(
        assignment, agent, _mail_payload(), db=session, trigger_context=dict(MAIL_CONTEXT), skip_if_active=True
    )

    assert session.lock_scopes() == [f"assignment_trigger:{assignment.id}", f"agent_event_dedup:{agent.id}"]


@pytest.mark.asyncio
async def test_a_delegated_pick_up_of_the_mail_is_refused_too(monkeypatch, _engine):
    agent = _agent()
    pick_up = _assignment(agent, name="Mail pick-up")
    original = uuid.uuid4()
    asked: list = []
    _patch_duplicate(monkeypatch, original, asked)
    session = _FakeSession()
    user = uuid.uuid4()

    run_id = await ae.trigger_assignment(
        pick_up,
        agent,
        {"trigger_type": "delegation", "task": "Reply to <KNOX@mail.example> on the interview thread", "delegated_by_agent_id": "x"},
        db=session,
        triggered_by_user_id=user,
    )

    run = session.added[0]
    assert str(run.id) == run_id and run.status == "skipped"
    assert asked == [(agent.id, "mail:knox@mail.example", 600)]
    assert run.triggered_by_user_id == user and run.user_id == user
    assert _engine.created == []


@pytest.mark.asyncio
async def test_an_agent_delegating_its_own_mail_to_a_sibling_assignment_is_not_refused(monkeypatch, _engine):
    """The mail run of agent A hands the reply to another assignment of A and
    names the Message-ID: that is A's own chain, not a second pick-up."""
    agent = _agent()
    sibling = _assignment(agent, name="Reply drafter")

    async def _explode(*_args, **_kwargs):
        raise AssertionError("an agent's own delegation is not keyed, so it is never checked")

    monkeypatch.setattr(ae, "duplicate_event_run", _explode)
    session = _FakeSession()

    run_id = await ae.trigger_assignment(
        sibling,
        agent,
        {
            "trigger_type": "delegation",
            "task": "Draft the reply to <KNOX@mail.example>",
            "delegated_by_agent_id": str(agent.id),
        },
        db=session,
    )

    assert run_id is not None and session.added[0].status == "pending"
    assert dedup.DEDUP_KEY_CONTEXT not in (session.added[0].trigger_context or {})
    assert session.lock_scopes() == []


@pytest.mark.asyncio
async def test_the_assignment_window_overrides_the_default_and_zero_turns_it_off(monkeypatch, _engine):
    agent = _agent()
    asked: list = []
    _patch_duplicate(monkeypatch, uuid.uuid4(), asked)

    narrow = _assignment(agent, trigger_config={"dedup_window_seconds": 30})
    session = _FakeSession()
    await ae.trigger_assignment(narrow, agent, _mail_payload(), db=session, trigger_context=dict(MAIL_CONTEXT))
    assert asked == [(agent.id, "mail:knox@mail.example", 30)]
    assert session.added[0].status == "skipped"

    off = _assignment(agent, trigger_config={"dedup_window_seconds": 0})
    session = _FakeSession()
    run_id = await ae.trigger_assignment(off, agent, _mail_payload(), db=session, trigger_context=dict(MAIL_CONTEXT))
    # Not asked, no agent lock, the run starts; its key is still recorded.
    assert len(asked) == 1
    assert session.lock_scopes() == []
    assert run_id is not None and session.added[0].status == "pending"
    assert session.added[0].trigger_context[dedup.DEDUP_KEY_CONTEXT] == "mail:knox@mail.example"


@pytest.mark.asyncio
async def test_a_scheduled_or_webhook_run_is_never_checked(monkeypatch, _engine):
    agent = _agent()
    assignment = _assignment(agent)

    async def _explode(*_args, **_kwargs):
        raise AssertionError("duplicate_event_run must not be consulted for a run without a key")

    monkeypatch.setattr(ae, "duplicate_event_run", _explode)

    for context in ({"type": "schedule"}, {"type": "webhook"}, None):
        session = _FakeSession()
        run_id = await ae.trigger_assignment(assignment, agent, {"alert": 1}, db=session, trigger_context=context)
        assert run_id is not None and session.added[0].status == "pending"
        assert session.lock_scopes() == []
        assert dedup.DEDUP_KEY_CONTEXT not in (session.added[0].trigger_context or {})


@pytest.mark.asyncio
async def test_a_mail_without_any_identity_is_not_checked(monkeypatch, _engine):
    agent = _agent()
    assignment = _assignment(agent)

    async def _explode(*_args, **_kwargs):
        raise AssertionError("no key, no check")

    monkeypatch.setattr(ae, "duplicate_event_run", _explode)
    session = _FakeSession()
    run_id = await ae.trigger_assignment(
        assignment, agent, {"from": "a@b", "message_id": "AAMk"}, db=session, trigger_context=dict(MAIL_CONTEXT)
    )
    assert run_id is not None and session.added[0].status == "pending"


def test_the_dedup_agent_is_read_tolerantly_from_the_assignment():
    """The key derivation sits on every dispatch path before the slot and queue
    decision; an object without ``agent_id`` keys the event as agent-less
    instead of raising."""
    agent = _agent()
    assert ae._dedup_agent_id(_assignment(agent)) == agent.id
    assert ae._dedup_agent_id(SimpleNamespace(id=uuid.uuid4(), trigger_config={})) is None


@pytest.mark.asyncio
async def test_a_bare_assignment_double_on_the_queue_path_is_not_dropped(monkeypatch, _engine):
    """A webhook delivery through ``trigger_or_queue_assignment`` on an assignment
    object carrying no ``agent_id`` (the dispatch tests' doubles) reaches the
    queue decision: the dedup layer never raises on the dispatch path."""
    queued: list = []

    async def _explode(*_args, **_kwargs):
        raise AssertionError("a webhook run has no key: duplicate_event_run must not be consulted")

    async def _busy(_db, _assignment_id):
        return ae._SlotUsage(active=ae.assignment_dispatch.MAX_CONCURRENCY_CAP, busy_keys=frozenset())

    async def _depth(_db, _assignment_id):
        return 0

    async def _cap(_assignment):
        return 1

    async def _create(_db, _assignment, _agent, payload, context):
        queued.append((payload, context))
        return "run-queued"

    monkeypatch.setattr(ae, "duplicate_event_run", _explode)
    monkeypatch.setattr(ae, "_slot_usage", _busy)
    monkeypatch.setattr(ae, "count_queued_runs", _depth)
    monkeypatch.setattr(ae, "_assignment_cap", _cap)
    monkeypatch.setattr(ae, "_create_queued_run", _create)
    bare = SimpleNamespace(
        id=uuid.uuid4(), name="WF", enabled=True, is_company_managed=False, user_id=None, trigger_config={}
    )
    session = _FakeSession()

    result = await ae.trigger_or_queue_assignment(
        bare, SimpleNamespace(name="A", enabled=True), {"m": 1}, db=session, trigger_context={"type": "webhook"}
    )

    assert (result.status, result.run_id) == ("queued", "run-queued")
    assert queued == [({"m": 1}, {"type": "webhook"})]
    assert session.lock_scopes() == [f"assignment_trigger:{bare.id}"]


# -- trigger_or_queue_assignment (the polled path) ---------------------------


def _patch_slots(monkeypatch):
    async def _usage(_db, _assignment_id):
        return ae._SlotUsage(active=0, busy_keys=frozenset(), exclusive=False)

    async def _depth(_db, _assignment_id):
        return 0

    async def _cap(_assignment):
        return 10

    monkeypatch.setattr(ae, "_slot_usage", _usage)
    monkeypatch.setattr(ae, "count_queued_runs", _depth)
    monkeypatch.setattr(ae, "_assignment_cap", _cap)


@pytest.mark.asyncio
async def test_a_polled_copy_of_a_notified_mail_answers_duplicate(monkeypatch, _engine):
    """The poll behind a Graph notification fetches the mail the notification ran."""
    agent = _agent()
    assignment = _assignment(agent)
    original = uuid.uuid4()
    _patch_duplicate(monkeypatch, original)
    _patch_slots(monkeypatch)
    session = _FakeSession()

    result = await ae.trigger_or_queue_assignment(
        assignment, agent, _mail_payload(), db=session, trigger_context=dict(MAIL_CONTEXT), overflow="reject"
    )

    assert result.status == "duplicate"
    run = session.added[0]
    assert result.run_id == str(run.id) and run.status == "skipped"
    assert run.trigger_context[dedup.DUPLICATE_OF_CONTEXT] == str(original)
    assert session.lock_scopes() == [f"assignment_trigger:{assignment.id}", f"agent_event_dedup:{agent.id}"]
    assert session.commits == 1
    assert _engine.created == []
    assert _engine.counted == [(assignment.id, 1)]


@pytest.mark.asyncio
async def test_a_polled_first_mail_starts_with_its_key_checked_once(monkeypatch, _engine):
    agent = _agent()
    assignment = _assignment(agent)
    asked: list = []
    _patch_duplicate(monkeypatch, None, asked)
    _patch_slots(monkeypatch)
    session = _FakeSession()

    result = await ae.trigger_or_queue_assignment(
        assignment, agent, _mail_payload(), db=session, trigger_context=dict(MAIL_CONTEXT), overflow="reject"
    )

    assert result.status == "ok" and result.run_id is not None
    # Checked by the queue path; trigger_assignment, called with the key already
    # in the context, does not ask again (the lock is taken once per path).
    assert asked == [(agent.id, "mail:knox@mail.example", 600)]
    assert session.added[0].trigger_context[dedup.DEDUP_KEY_CONTEXT] == "mail:knox@mail.example"
    # The dedup key is not the ordering key: the thread is one, read
    # from the trigger's provider and event; this bare trigger config has none.
    assert "ordering_key" not in session.added[0].trigger_context
    assert len(_engine.created) == 1


# -- configured dispatch dedup key (#10622) ---------------------------------

CHECK_RUN_CONTEXT = {"type": "webhook", "scope": "provider", "provider": "github", "event_key": "check_run.completed"}
GROUP_DEDUP = {
    "dedup": {
        "key": "{{payload.repository.full_name}}@{{payload.check_run.head_sha}}",
        "window_seconds": 120,
        "group": "green",
    }
}


def _check_run(sha: str = "ABC123") -> dict:
    return {"repository": {"full_name": "Org/Repo"}, "check_run": {"head_sha": sha, "id": 1}}


@pytest.mark.asyncio
async def test_a_configured_key_starts_the_first_delivery_with_its_window(monkeypatch, _engine):
    agent = _agent()
    green = _assignment(agent, name="green check_run", trigger_config=GROUP_DEDUP)
    asked: list = []
    _patch_duplicate(monkeypatch, None, asked)
    session = _FakeSession()

    run_id = await ae.trigger_assignment(
        green, agent, _check_run(), db=session, trigger_context=dict(CHECK_RUN_CONTEXT)
    )

    assert run_id is not None and session.added[0].status == "pending"
    key = "cfg:group:green:org/repo@abc123"
    assert session.added[0].trigger_context[dedup.DEDUP_KEY_CONTEXT] == key
    assert asked == [(agent.id, key, 120)]
    assert _engine.reasons == []


@pytest.mark.asyncio
async def test_a_second_assignment_of_the_group_on_the_same_head_is_dropped_with_its_reason(monkeypatch, _engine):
    agent = _agent()
    workflow_listener = _assignment(agent, name="green workflow_run", trigger_config=GROUP_DEDUP)
    original = uuid.uuid4()
    asked: list = []
    _patch_duplicate(monkeypatch, original, asked)
    session = _FakeSession()

    run_id = await ae.trigger_assignment(
        workflow_listener, agent, _check_run("abc123"), db=session, trigger_context=dict(CHECK_RUN_CONTEXT)
    )

    assert run_id is not None and session.added[0].status == "skipped"
    assert asked == [(agent.id, "cfg:group:green:org/repo@abc123", 120)]
    assert _engine.created == []
    assert _engine.counted == [(workflow_listener.id, 1)]
    assert _engine.reasons == [(workflow_listener.id, {dedup.CONFIGURED_DROP_REASON: 1})]


@pytest.mark.asyncio
async def test_without_a_configured_dedup_a_webhook_delivery_is_not_keyed(monkeypatch, _engine):
    agent = _agent()
    plain = _assignment(agent, name="no dedup")
    asked: list = []
    _patch_duplicate(monkeypatch, uuid.uuid4(), asked)
    session = _FakeSession()

    run_id = await ae.trigger_assignment(
        plain, agent, _check_run(), db=session, trigger_context=dict(CHECK_RUN_CONTEXT)
    )

    assert run_id is not None and session.added[0].status == "pending"
    assert asked == []
    assert dedup.DEDUP_KEY_CONTEXT not in session.added[0].trigger_context


@pytest.mark.asyncio
async def test_a_configured_key_naming_an_absent_field_starts_the_run(monkeypatch, _engine):
    agent = _agent()
    green = _assignment(agent, trigger_config=GROUP_DEDUP)
    asked: list = []
    _patch_duplicate(monkeypatch, uuid.uuid4(), asked)
    session = _FakeSession()

    run_id = await ae.trigger_assignment(
        green, agent, {"repository": {"full_name": "Org/Repo"}}, db=session, trigger_context=dict(CHECK_RUN_CONTEXT)
    )

    assert run_id is not None and session.added[0].status == "pending"
    assert asked == []
