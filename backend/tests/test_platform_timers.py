"""Per-object timers (``arm_timer``): the ``timer.due`` platform event raised at a date a run sets."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.platform_timer import (
    TIMER_ARMED,
    TIMER_FIRED,
    TIMER_FIRED_UNHEARD,
    TIMER_SKIPPED,
    TIMER_STATES,
    PlatformTimer,
)
from app.services import platform_events, platform_timers
from app.services.builtin_tools.timers import TIMER_TOOL_NAMES, build_timer_tools
from app.services.platform_timers import (
    TIMER_DUE_EVENT,
    TimerError,
    check_payload,
    parse_due,
    timer_due_data,
)

_NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def _timer(**overrides) -> PlatformTimer:
    values = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "correlation_key": "schedule:thread-42",
        "due_at": _NOW - timedelta(seconds=5),
        "payload": {"thread": "42", "mark": "3bd"},
        "unless_work_item_id": None,
        "unless_shared_state_key": None,
        "state": TIMER_ARMED,
        "created_at": _NOW - timedelta(days=3),
    }
    values.update(overrides)
    return PlatformTimer(**values)


# -- Parsing ---------------------------------------------------------------------


def test_due_at_needs_an_offset_and_a_future_time():
    assert parse_due("2026-11-05T14:00:00+01:00", now=_NOW) == datetime(2026, 11, 5, 13, 0, tzinfo=UTC)
    assert parse_due("2026-11-05T13:00:00Z", now=_NOW) == datetime(2026, 11, 5, 13, 0, tzinfo=UTC)
    with pytest.raises(TimerError, match="offset"):
        parse_due("2026-11-05T14:00:00", now=_NOW)
    with pytest.raises(TimerError, match="not in the future"):
        parse_due("2026-11-05T11:00:00+00:00", now=_NOW)
    with pytest.raises(TimerError, match="days ahead"):
        parse_due("2028-11-05T11:00:00+00:00", now=_NOW)
    with pytest.raises(TimerError, match="required"):
        parse_due("", now=_NOW)


def test_payload_is_a_bounded_json_object():
    assert check_payload(None) == {}
    assert check_payload('{"a": 1}') == {"a": 1}
    with pytest.raises(TimerError, match="JSON object"):
        check_payload("[1, 2]")
    with pytest.raises(TimerError, match="at most"):
        check_payload({"x": "y" * (platform_timers.MAX_PAYLOAD_CHARS + 1)})


# -- The event -------------------------------------------------------------------


def test_timer_due_is_in_the_catalog_with_its_filter_keys():
    spec = platform_events.PLATFORM_EVENTS[TIMER_DUE_EVENT]
    assert "correlation_key" in spec["filter_keys"]
    assert "payload.*" in spec["filter_keys"]
    assert TIMER_DUE_EVENT not in platform_events.PLATFORM_WIDE_EVENTS


def _sub(agent_id, user_id, flt):
    return platform_events.Subscription(
        assignment_id=uuid.uuid4(),
        agent_id=agent_id,
        events=(TIMER_DUE_EVENT,),
        filter=flt,
        scope_all=False,
        user_id=user_id,
    )


def test_a_starts_with_filter_takes_the_arming_agents_timer_only():
    timer = _timer()
    payload = {**timer_due_data(timer), "event": TIMER_DUE_EVENT, "event_id": str(timer.id)}
    flt = {"correlation_key": {"starts_with": "schedule:"}}

    assert platform_events.mismatch_reason(_sub(timer.agent_id, timer.user_id, flt), TIMER_DUE_EVENT, payload) is None
    # Another prefix: filtered out.
    other = {"correlation_key": {"starts_with": "calendar:"}}
    assert platform_events.mismatch_reason(_sub(timer.agent_id, timer.user_id, other), TIMER_DUE_EVENT, payload)
    # Another agent of the same person (scope own): not a participant.
    assert platform_events.mismatch_reason(_sub(uuid.uuid4(), timer.user_id, flt), TIMER_DUE_EVENT, payload)
    # The same agent run for another person: a personal object of someone else.
    assert platform_events.mismatch_reason(_sub(timer.agent_id, uuid.uuid4(), flt), TIMER_DUE_EVENT, payload)


def test_the_payload_keys_are_filterable():
    timer = _timer()
    payload = {**timer_due_data(timer), "event": TIMER_DUE_EVENT}
    sub = _sub(timer.agent_id, timer.user_id, {"payload.mark": "3bd"})
    assert platform_events.mismatch_reason(sub, TIMER_DUE_EVENT, payload) is None
    sub = _sub(timer.agent_id, timer.user_id, {"payload.mark": "t24h"})
    assert platform_events.mismatch_reason(sub, TIMER_DUE_EVENT, payload)


# -- Firing ----------------------------------------------------------------------


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _Session:
    """First execute: the due ids; then one locked re-read per id."""

    def __init__(self, timers, rereads=None):
        rereads = timers if rereads is None else rereads
        self._results = [_Rows([t.id for t in timers]), *(_Rows([r] if r else []) for r in rereads)]
        self.commits = 0
        self.rollbacks = 0
        self.states_at_commit: list[str] = []
        self._watched = [r for r in rereads if r]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _statement):
        return self._results.pop(0)

    async def commit(self):
        self.commits += 1
        self.states_at_commit.append(",".join(t.state for t in self._watched))

    async def rollback(self):
        self.rollbacks += 1


def _patch(monkeypatch, session, published, unless=None, heard=True, others=()):
    from app.core import database

    async def _publish(name, data, *, event_id=None):
        # The fired state is committed before the event is raised.
        published.append((name, data, event_id, session.commits))
        return event_id

    async def _unless(timer):
        return (unless or {}).get(timer.id)

    async def _listeners(data):
        if isinstance(heard, Exception):
            raise heard
        sub = _sub(uuid.UUID(data["agent_id"]), uuid.UUID(data["user_id"]), {})
        return [sub], ([sub] if heard else [])

    monkeypatch.setattr(database, "async_session", lambda: session)
    monkeypatch.setattr(platform_events, "publish", _publish)
    monkeypatch.setattr(platform_timers, "unless_reason", _unless)
    monkeypatch.setattr(platform_timers, "timer_listeners", _listeners)
    _others(monkeypatch, others)


def _others(monkeypatch, others=(), company_managed=False):
    async def _managed(_agent_id):
        return company_managed

    # The arming agent's company-managed flag (#2235): a personal agent unless a test says otherwise.
    monkeypatch.setattr(platform_timers, "agent_is_company_managed", _managed)

    async def _other(data):
        assert data["object_type"] == "timer"
        if isinstance(others, Exception):
            raise others
        return list(others)

    monkeypatch.setattr(platform_timers, "other_listeners", _other)


@pytest.mark.asyncio
async def test_a_due_timer_fires_once_after_its_state_is_committed(monkeypatch):
    timer = _timer()
    session = _Session([timer])
    published = []
    _patch(monkeypatch, session, published)

    fired = await platform_timers.fire_due_timers(now=_NOW)

    assert fired == 1
    assert timer.state == TIMER_FIRED and timer.event_id == str(timer.id)
    name, data, event_id, commits_before = published[0]
    assert name == TIMER_DUE_EVENT and event_id == str(timer.id)
    assert commits_before == 1
    assert data["correlation_key"] == "schedule:thread-42"
    assert data["payload"] == {"thread": "42", "mark": "3bd"}
    assert data["participant_agent_ids"] == [str(timer.agent_id)]


@pytest.mark.asyncio
async def test_a_timer_no_assignment_takes_fires_unheard_and_says_so(monkeypatch, caplog):
    timer = _timer()
    session = _Session([timer])
    published = []
    _patch(monkeypatch, session, published, heard=False)

    with caplog.at_level("WARNING", logger="app.services.platform_timers"):
        fired = await platform_timers.fire_due_timers(now=_NOW)

    # Still raised (flows and task watches read their own subscriptions), payload unchanged.
    assert fired == 1 and len(published) == 1
    name, data, event_id, commits_before = published[0]
    assert name == TIMER_DUE_EVENT and event_id == str(timer.id) and commits_before == 1
    assert data == timer_due_data(timer)
    assert timer.state == TIMER_FIRED_UNHEARD and session.states_at_commit == [TIMER_FIRED_UNHEARD]
    assert "no platform_event assignment, task watch or flow takes it" in timer.outcome
    assert "fired unheard" in caplog.text and "schedule:thread-42" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("other", ["task watch w1", "flow f1"])
async def test_a_timer_a_task_watch_or_a_flow_takes_is_fired_not_unheard(monkeypatch, caplog, other):
    # dispatch_event delivers timer.due to task watches and flows too: one that takes it is a listener.
    timer = _timer()
    session = _Session([timer])
    published = []
    _patch(monkeypatch, session, published, heard=False, others=[other])

    with caplog.at_level("WARNING", logger="app.services.platform_timers"):
        assert await platform_timers.fire_due_timers(now=_NOW) == 1

    assert timer.state == TIMER_FIRED and len(published) == 1
    assert "fired unheard" not in caplog.text


@pytest.mark.asyncio
async def test_unreadable_task_watches_or_flows_never_record_an_unheard_fire(monkeypatch):
    timer = _timer()
    session = _Session([timer])
    published = []
    _patch(monkeypatch, session, published, heard=False, others=RuntimeError("db down"))

    assert await platform_timers.fire_due_timers(now=_NOW) == 1
    assert timer.state == TIMER_FIRED and len(published) == 1


@pytest.mark.asyncio
async def test_unreadable_listeners_never_block_a_fire(monkeypatch):
    timer = _timer()
    session = _Session([timer])
    published = []
    _patch(monkeypatch, session, published, heard=RuntimeError("db down"))

    assert await platform_timers.fire_due_timers(now=_NOW) == 1
    assert timer.state == TIMER_FIRED and len(published) == 1


def test_fired_unheard_is_an_ended_state():
    assert TIMER_FIRED_UNHEARD in TIMER_STATES and TIMER_FIRED_UNHEARD != TIMER_ARMED


# -- Listeners -------------------------------------------------------------------


def _live(monkeypatch, subs, others=()):
    async def _subs(name):
        assert name == TIMER_DUE_EVENT
        return list(subs)

    monkeypatch.setattr(platform_events, "live_subscriptions", _subs)
    _others(monkeypatch, others)


@pytest.mark.asyncio
async def test_timer_listeners_test_scope_and_filter_as_the_dispatcher_does(monkeypatch):
    timer = _timer()
    mine = _sub(timer.agent_id, timer.user_id, {"correlation_key": {"starts_with": "schedule:"}})
    other_prefix = _sub(timer.agent_id, timer.user_id, {"correlation_key": {"starts_with": "calendar:"}})
    other_agent = _sub(uuid.uuid4(), timer.user_id, {})
    _live(monkeypatch, [mine, other_prefix, other_agent])

    in_scope, taking = await platform_timers.timer_listeners(timer_due_data(timer))

    assert in_scope == [mine, other_prefix]
    assert taking == [mine]


@pytest.mark.asyncio
async def test_no_warning_when_a_listener_takes_the_key(monkeypatch):
    timer = _timer()
    _live(monkeypatch, [_sub(timer.agent_id, timer.user_id, {"correlation_key": {"starts_with": "schedule:"}})])

    serialized = platform_timers.serialize_timer(timer)
    assert await platform_timers.unheard_warning(serialized, user_id=str(timer.user_id)) is None


@pytest.mark.asyncio
async def test_the_warning_names_the_key_and_the_filters_heard(monkeypatch):
    timer = _timer(correlation_key="followup:x")
    flt = {"correlation_key": {"starts_with": "schedule:"}}
    _live(monkeypatch, [_sub(timer.agent_id, timer.user_id, flt), _sub(timer.agent_id, timer.user_id, flt)])

    warning = await platform_timers.unheard_warning(platform_timers.serialize_timer(timer), user_id=str(timer.user_id))

    assert warning.startswith("Warning:") and "`followup:x`" in warning
    assert TIMER_FIRED_UNHEARD in warning
    # The same filter twice is named once.
    assert warning.count('{"correlation_key": {"starts_with": "schedule:"}}') == 1


@pytest.mark.asyncio
async def test_the_warning_says_when_the_agent_has_no_listener_at_all(monkeypatch):
    timer = _timer()
    _live(monkeypatch, [_sub(uuid.uuid4(), timer.user_id, {})])

    warning = await platform_timers.unheard_warning(platform_timers.serialize_timer(timer), user_id=str(timer.user_id))

    assert "no enabled platform_event assignment" in warning


@pytest.mark.asyncio
async def test_no_warning_when_a_task_watch_or_a_flow_takes_the_key(monkeypatch):
    timer = _timer(correlation_key="followup:x")
    _live(monkeypatch, [_sub(timer.agent_id, timer.user_id, {"correlation_key": "schedule:"})], others=["flow f1"])

    assert (
        await platform_timers.unheard_warning(platform_timers.serialize_timer(timer), user_id=str(timer.user_id))
        is None
    )


@pytest.mark.asyncio
async def test_other_listeners_ask_the_task_watch_and_flow_dispatchers(monkeypatch):
    from app.services import flow_triggers, platform_event_task_watches

    timer = _timer()
    seen = []

    async def _watches(name, payload):
        seen.append(("watches", name, payload["event"], payload["correlation_key"]))
        return [SimpleNamespace(id="w1")]

    async def _flows(name, payload):
        seen.append(("flows", name, payload["event"], payload["correlation_key"]))
        return ["f1"]

    monkeypatch.setattr(platform_event_task_watches, "watches_taking", _watches)
    monkeypatch.setattr(flow_triggers, "flows_taking_platform_event", _flows)

    assert await platform_timers.other_listeners(timer_due_data(timer)) == ["task watch w1", "flow f1"]
    assert seen == [
        ("watches", TIMER_DUE_EVENT, TIMER_DUE_EVENT, "schedule:thread-42"),
        ("flows", TIMER_DUE_EVENT, TIMER_DUE_EVENT, "schedule:thread-42"),
    ]


@pytest.mark.asyncio
async def test_unreadable_listeners_give_no_warning(monkeypatch):
    async def _boom(_name):
        raise RuntimeError("db down")

    monkeypatch.setattr(platform_events, "live_subscriptions", _boom)
    _others(monkeypatch, ())
    timer = _timer()
    assert await platform_timers.unheard_warning(platform_timers.serialize_timer(timer), user_id="u") is None


@pytest.mark.asyncio
async def test_a_timer_whose_unless_is_met_is_skipped_never_fired(monkeypatch):
    timer = _timer(unless_work_item_id=uuid.uuid4())
    session = _Session([timer])
    published = []
    _patch(monkeypatch, session, published, unless={timer.id: "unless: work item x is done"})

    fired = await platform_timers.fire_due_timers(now=_NOW)

    assert fired == 0 and published == []
    assert timer.state == TIMER_SKIPPED
    assert "done" in timer.outcome


@pytest.mark.asyncio
async def test_a_timer_taken_by_another_process_is_left_alone(monkeypatch):
    timer = _timer()
    # Locked (SKIP LOCKED) or already fired between the read and the lock.
    session = _Session([timer], rereads=[None])
    published = []
    _patch(monkeypatch, session, published)

    assert await platform_timers.fire_due_timers(now=_NOW) == 0
    assert published == [] and session.rollbacks == 1 and timer.state == TIMER_ARMED


@pytest.mark.asyncio
async def test_unless_reads_the_work_item_and_the_shared_state(monkeypatch):
    from app.services import shared_state, work_item

    item_id = uuid.uuid4()
    statuses = {"item": "done", "state": "active"}

    async def _get_item(work_item_id, *, user_id, reader=None):
        return {"id": work_item_id, "status": statuses["item"]}

    async def _get_state(*, user_id, key, reader=None):
        return {"key": key, "status": statuses["state"]}

    monkeypatch.setattr(work_item, "get_work_item", _get_item)
    monkeypatch.setattr(shared_state, "get_state", _get_state)

    assert "done" in await platform_timers.unless_reason(_timer(unless_work_item_id=item_id))
    statuses["item"] = "in_progress"
    assert await platform_timers.unless_reason(_timer(unless_work_item_id=item_id)) is None
    assert await platform_timers.unless_reason(_timer(unless_shared_state_key="calendar:evt-1")) is None
    statuses["state"] = "resolved"
    assert "resolved" in await platform_timers.unless_reason(_timer(unless_shared_state_key="calendar:evt-1"))


# -- Arm, re-arm, cancel ---------------------------------------------------------


class _ArmSession:
    """Arm: the locked read of the armed timer of the key, then (no previous) the count."""

    def __init__(self, previous=None, armed_count=0):
        self.previous = previous
        self.armed_count = armed_count
        self.added = []
        self.commits = 0
        self.flushes = 0
        self._calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _statement):
        self._calls += 1
        if self._calls == 1:
            return _Rows([self.previous] if self.previous else [])
        return SimpleNamespace(scalar_one=lambda: self.armed_count)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


@pytest.mark.asyncio
async def test_arming_stores_the_timer(monkeypatch):
    from app.core import database

    session = _ArmSession()
    monkeypatch.setattr(database, "async_session", lambda: session)
    user, agent = str(uuid.uuid4()), str(uuid.uuid4())

    timer, replaced = await platform_timers.arm_timer(
        user_id=user,
        agent_id=agent,
        correlation_key=" schedule:thread-42 ",
        due_at="2026-11-08T09:00:00+01:00",
        payload={"thread": "42"},
        now=_NOW,
    )

    assert replaced is None and session.commits == 1
    assert timer["correlation_key"] == "schedule:thread-42"
    assert timer["due_at"] == "2026-11-08T08:00:00+00:00"
    assert timer["state"] == TIMER_ARMED
    assert session.added[0].agent_id == uuid.UUID(agent)


@pytest.mark.asyncio
async def test_arming_the_same_key_again_replaces_the_earlier_timer(monkeypatch):
    from app.core import database

    previous = _timer(due_at=_NOW + timedelta(days=1))
    session = _ArmSession(previous=previous)
    monkeypatch.setattr(database, "async_session", lambda: session)

    timer, replaced = await platform_timers.arm_timer(
        user_id=str(previous.user_id),
        agent_id=str(previous.agent_id),
        correlation_key=previous.correlation_key,
        due_at="2026-11-09T09:00:00+00:00",
        now=_NOW,
    )

    assert previous.state == "replaced" and replaced["id"] == str(previous.id)
    # The old row leaves 'armed' before the new one is added (partial unique index).
    assert session.flushes == 1
    assert timer["id"] != str(previous.id) and timer["state"] == TIMER_ARMED


@pytest.mark.asyncio
async def test_arming_past_the_cap_is_refused(monkeypatch):
    from app.core import database

    session = _ArmSession(armed_count=platform_timers.MAX_ARMED_PER_AGENT)
    monkeypatch.setattr(database, "async_session", lambda: session)
    with pytest.raises(TimerError, match="cancel"):
        await platform_timers.arm_timer(
            user_id=str(uuid.uuid4()),
            agent_id=str(uuid.uuid4()),
            correlation_key="schedule:x",
            due_at="2026-11-09T09:00:00+00:00",
            now=_NOW,
        )
    assert session.added == []


@pytest.mark.asyncio
async def test_cancel_ends_the_armed_timer(monkeypatch):
    from app.core import database

    timer = _timer(due_at=_NOW + timedelta(hours=2))

    class _CancelSession(_ArmSession):
        async def execute(self, _statement):
            return _Rows([timer])

    session = _CancelSession()
    monkeypatch.setattr(database, "async_session", lambda: session)

    out = await platform_timers.cancel_timer(
        user_id=str(timer.user_id), agent_id=str(timer.agent_id), correlation_key=timer.correlation_key
    )

    assert out["state"] == "cancelled" and timer.state == "cancelled" and session.commits == 1


# -- List ------------------------------------------------------------------------


class _ListSession(_ArmSession):
    """List: records the SELECT so the filter it carries can be read."""

    def __init__(self, rows=()):
        super().__init__()
        self.rows = list(rows)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Rows(self.rows)


def _sql(statement) -> str:
    from sqlalchemy.dialects import postgresql

    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


async def _list_statement(monkeypatch, **kwargs):
    from app.core import database

    session = _ListSession()
    monkeypatch.setattr(database, "async_session", lambda: session)
    await platform_timers.list_timers(user_id=str(uuid.uuid4()), agent_id=str(uuid.uuid4()), **kwargs)
    return session.statements[0]


async def _list_sql(monkeypatch, **kwargs) -> str:
    return _sql(await _list_statement(monkeypatch, **kwargs))


async def _like_pattern(monkeypatch, **kwargs) -> str:
    from sqlalchemy.dialects import postgresql

    params = (await _list_statement(monkeypatch, **kwargs)).compile(dialect=postgresql.dialect()).params
    (pattern,) = [v for k, v in params.items() if k.startswith("correlation_key")]
    return pattern


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["armed", "all"])
async def test_a_base64_key_prefix_matches_literally(monkeypatch, state):
    pattern = await _like_pattern(monkeypatch, state=state, key_prefix="brief:a+b/c%_d=")
    # '=', '+' and '/' pass as they are; the LIKE wildcards are escaped; one trailing '%'.
    assert pattern == "brief:a+b/c\\%\\_d=%"


@pytest.mark.asyncio
async def test_the_prefix_is_normalized_like_the_key_it_matches(monkeypatch):
    long_key = "brief:" + "A" * 300 + "="
    pattern = await _like_pattern(monkeypatch, state="all", key_prefix=f"  {long_key}  ")
    stored = platform_timers.normalize_correlation_key(long_key)
    assert len(stored) == 200 and pattern == f"{stored}%"


@pytest.mark.asyncio
async def test_state_all_reads_armed_and_ended_timers(monkeypatch):
    sql = await _list_sql(monkeypatch, state="all", key_prefix="brief:abc=")
    assert "platform_timers.state" not in sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
    assert "platform_timers.state =" in await _list_sql(monkeypatch, state="armed")
    assert "platform_timers.state !=" in await _list_sql(monkeypatch, state="ended")


@pytest.mark.asyncio
async def test_list_tool_lists_the_timer_under_its_full_prefix(monkeypatch):
    from app.tools.base import ToolCall

    armed = _timer(correlation_key="brief:abc=:t60", due_at=_NOW + timedelta(hours=1))
    seen = []

    async def _fake_list(**kwargs):
        seen.append(kwargs)
        prefix = kwargs["key_prefix"] or ""
        return [platform_timers.serialize_timer(armed)] if armed.correlation_key.startswith(prefix) else []

    monkeypatch.setattr(platform_timers, "list_timers", _fake_list)
    _defs, executor = build_timer_tools(str(armed.user_id), str(armed.agent_id))
    for state in ("all", "armed"):
        call = ToolCall(id=state, name="list_timers", arguments={"key_prefix": "brief:abc=", "state": state})
        out = str((await executor.execute_tool(call)).content)
        assert "brief:abc=:t60" in out
    assert [k["state"] for k in seen] == ["all", "armed"]
    call = ToolCall(id="none", name="list_timers", arguments={"key_prefix": "other:", "state": "all"})
    out = str((await executor.execute_tool(call)).content)
    assert out.startswith("No timers") and "`other:`" in out


# -- Tools -----------------------------------------------------------------------


def test_the_three_tools_are_built_and_sourced_in_coordination():
    definitions, executor = build_timer_tools("", "")
    assert {d.name for d in definitions} == TIMER_TOOL_NAMES
    assert set(executor.tool_names) == TIMER_TOOL_NAMES
    assert all(d.source == "builtin:coordination" for d in definitions)


def test_the_tools_ride_with_the_shared_state_grant():
    from app.services.builtin_tool_aliases import BUILTIN_GROUP_ALIASES

    _group, functions = BUILTIN_GROUP_ALIASES["shared_state"]
    assert set(functions) >= TIMER_TOOL_NAMES


@pytest.mark.asyncio
@pytest.mark.parametrize("heard", [True, False])
async def test_arm_tool_arms_and_warns_only_when_no_listener_takes_the_key(monkeypatch, heard):
    from app.tools.base import ToolCall

    timer = _timer(due_at=_NOW + timedelta(days=1))
    armed = []

    async def _arm(**kwargs):
        armed.append(kwargs)
        return platform_timers.serialize_timer(timer), None

    flt = {"correlation_key": {"starts_with": "schedule:" if heard else "calendar:"}}
    _live(monkeypatch, [_sub(timer.agent_id, timer.user_id, flt)])  # no task watch, no flow
    monkeypatch.setattr(platform_timers, "arm_timer", _arm)
    _defs, executor = build_timer_tools(str(timer.user_id), str(timer.agent_id))
    call = ToolCall(
        id="1", name="arm_timer", arguments={"correlation_key": timer.correlation_key, "due_at": "2026-11-06T12:00:00Z"}
    )

    out = str((await executor.execute_tool(call)).content)

    assert len(armed) == 1 and out.startswith("Armed timer")
    assert ("Warning:" in out) is (not heard)
    if not heard:
        assert "`schedule:thread-42`" in out and "calendar:" in out


@pytest.mark.asyncio
async def test_arm_tool_reports_a_refusal_without_raising(monkeypatch):
    from app.tools.base import ToolCall

    _defs, executor = build_timer_tools(str(uuid.uuid4()), str(uuid.uuid4()))
    call = ToolCall(id="1", name="arm_timer", arguments={"correlation_key": "schedule:x", "due_at": "tomorrow"})
    out = (await executor.execute_tool(call)).content
    assert "Error:" in str(out) and "Nothing was armed" in str(out)
