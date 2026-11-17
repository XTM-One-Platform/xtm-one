"""Assignment run dispatch: caps, ordering keys, fair promotion, backlog
interleaving, the 429 back-off, and a simulation of the queue under load.

The simulation drives the SAME planning functions the drain calls
(``plan_promotions`` / ``select_runs``) through a discrete-event model of
deliveries arriving, runs holding a slot and finishing, next to a model of the
dispatch it replaced (one run per assignment per 60 s poll tick), so the lag
figures quoted on the pull request are reproducible here.
"""

from __future__ import annotations

import heapq
import itertools
import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from app.services import assignment_dispatch as dispatch
from app.services.assignment_dispatch import QueuedRun, QueueState

_BASE = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Caps and ordering keys
# ---------------------------------------------------------------------------


def test_the_cap_is_the_assignment_s_own_else_the_platform_default(monkeypatch):
    monkeypatch.setattr(dispatch.settings, "assignment_max_concurrent_runs", 8)
    assert dispatch.assignment_concurrency_cap(None) == 8
    assert dispatch.assignment_concurrency_cap({}) == 8
    assert dispatch.assignment_concurrency_cap({"max_concurrent_runs": 3}) == 3
    assert dispatch.assignment_concurrency_cap({"max_concurrent_runs": "2"}) == 2


@pytest.mark.parametrize("raw", [0, -4, 10_000, True, "many", 2.5])
def test_a_cap_out_of_bounds_or_malformed_is_clamped_or_ignored(monkeypatch, raw):
    monkeypatch.setattr(dispatch.settings, "assignment_max_concurrent_runs", 8)
    cap = dispatch.assignment_concurrency_cap({"max_concurrent_runs": raw})
    assert 1 <= cap <= dispatch.MAX_CONCURRENCY_CAP
    if isinstance(raw, bool) or raw == "many":
        assert cap == 8


def test_the_platform_default_is_clamped_too(monkeypatch):
    monkeypatch.setattr(dispatch.settings, "assignment_max_concurrent_runs", 0)
    assert dispatch.default_concurrency_cap() == 1
    monkeypatch.setattr(dispatch.settings, "assignment_max_concurrent_runs", 500)
    assert dispatch.default_concurrency_cap() == dispatch.MAX_CONCURRENCY_CAP


def test_an_ordering_key_is_read_through_dicts_and_list_indexes():
    config = {"ordering_key": "repository.full_name"}
    assert dispatch.resolve_ordering_key(config, {"repository": {"full_name": "org/repo"}}) == "org/repo"
    config = {"ordering_key": "items.1.id"}
    assert dispatch.resolve_ordering_key(config, {"items": [{"id": 1}, {"id": 42}]}) == "42"
    assert dispatch.resolve_ordering_key({"ordering_key": "flag"}, {"flag": True}) == "true"


@pytest.mark.parametrize(
    "payload",
    [{}, {"thread": None}, {"thread": {"id": 1}}, {"thread": [1]}, {"thread": "  "}, "not a dict"],
)
def test_no_scalar_at_the_path_means_an_unordered_run(payload):
    assert dispatch.resolve_ordering_key({"ordering_key": "thread"}, payload) is None


def test_no_configured_path_means_an_unordered_run():
    assert dispatch.resolve_ordering_key(None, {"thread": "t"}) is None
    assert dispatch.resolve_ordering_key({"ordering_key": 3}, {"thread": "t"}) is None
    assert dispatch.resolve_ordering_key({"ordering_key": "  "}, {"thread": "t"}) is None


def test_a_long_ordering_key_is_hashed_so_two_values_never_share_a_prefix():
    config = {"ordering_key": "id"}
    first = dispatch.resolve_ordering_key(config, {"id": "x" * 300 + "a"})
    second = dispatch.resolve_ordering_key(config, {"id": "x" * 300 + "b"})
    assert first.startswith("sha256:") and second.startswith("sha256:")
    assert first != second


def test_the_ordering_key_rides_on_the_trigger_context():
    config = {"ordering_key": "conversation"}
    assert dispatch.with_ordering_key(config, {"conversation": "c1"}, {"type": "webhook"}) == {
        "type": "webhook",
        "ordering_key": "c1",
    }
    context = {"type": "webhook"}
    assert dispatch.with_ordering_key(config, {}, context) is context


@pytest.mark.parametrize(
    ("provider", "event", "payload"),
    [
        ("microsoft", "new_email", {"conversation_id": "AAQk-thread"}),
        ("google", "new_email", {"thread_id": "AAQk-thread"}),
    ],
)
def test_a_mail_trigger_is_ordered_by_its_thread_by_default(provider, event, payload):
    """Two mails of one thread never run side by side, now that an assignment
    runs up to ten deliveries at once."""
    assert dispatch.resolve_ordering_key({"provider": provider, "event": event}, payload) == "AAQk-thread"
    assert dispatch.resolve_ordering_key({"provider": provider, "event_type": event}, payload) == "AAQk-thread"


def test_an_explicit_ordering_key_or_a_blank_one_wins_over_the_mail_default():
    payload = {"conversation_id": "thread", "from": "someone@example.test"}
    config = {"provider": "microsoft", "event": "new_email"}
    assert dispatch.resolve_ordering_key({**config, "ordering_key": "from"}, payload) == "someone@example.test"
    assert dispatch.resolve_ordering_key({**config, "ordering_key": ""}, payload) is None


def test_a_chat_message_is_ordered_by_the_conversation_its_payload_names():
    """Two messages of one conversation never run side by side; a configured key wins."""
    payload = {dispatch.CONVERSATION_KEY_FIELD: "chat:19:abc", "from_name": "Sam"}
    config = {"provider": "any", "event": "any_chat_message"}
    assert dispatch.resolve_ordering_key(config, payload) == "chat:19:abc"
    assert dispatch.resolve_ordering_key(None, payload) == "chat:19:abc"
    assert dispatch.resolve_ordering_key({**config, "ordering_key": ""}, payload) is None
    assert dispatch.resolve_ordering_key({**config, "ordering_key": "from_name"}, payload) == "Sam"


def test_other_triggers_have_no_default_ordering_key():
    payload = {"conversation_id": "c", "thread_id": "t", "channel_id": "C1"}
    assert dispatch.resolve_ordering_key({"provider": "slack", "event": "new_slack_message"}, payload) is None
    assert dispatch.resolve_ordering_key({"path": "github"}, payload) is None


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ({"type": "webhook"}, True),
        ({"type": "integration_event", "provider": "microsoft"}, True),
        ({"type": "manual"}, False),
        ({"type": "followup"}, False),
        ({"type": "schedule"}, False),
        ({"type": "chain"}, False),
        ({}, False),
        (None, False),
    ],
)
def test_only_webhooks_and_integration_events_are_deliveries(context, expected):
    assert dispatch.is_delivery(context) is expected


@pytest.mark.parametrize(
    ("run_type", "expected"),
    [
        ("webhook", True),
        ("integration_event", True),
        ("platform_event", True),
        ("manual", False),
        ("followup", False),
        ("schedule", False),
        ("chain", False),
        (None, False),
    ],
)
def test_platform_events_share_an_assignment_up_to_its_cap(run_type, expected):
    """A platform event run in flight never holds the assignment to itself:
    its subscriber takes the next event beside it, up to the cap."""
    assert dispatch.shares_slots(run_type) is expected


def test_a_platform_event_is_not_a_delivery():
    """Sharing slots changes nothing else: no quota pre-check, no chat coalescing."""
    assert dispatch.is_delivery({"type": "platform_event"}) is False


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _state(
    *,
    agent=None,
    depth=5,
    active=0,
    cap=8,
    waited=60.0,
    held=False,
    exclusive=False,
    startable=None,
    assignment=None,
) -> QueueState:
    return QueueState(
        assignment_id=assignment or uuid.uuid4(),
        agent_id=agent or uuid.uuid4(),
        depth=depth,
        oldest_at=_BASE - timedelta(seconds=waited),
        active=active,
        cap=cap,
        held=held,
        exclusive=exclusive,
        startable=startable,
    )


def test_free_slots_are_bounded_by_the_cap_and_the_backlog():
    assert _state(depth=5, active=2, cap=8).free == 5
    assert _state(depth=20, active=2, cap=8).free == 6
    assert _state(depth=5, active=9, cap=8).free == 0


def test_free_slots_count_only_the_runs_that_can_start_now():
    assert _state(depth=20, active=2, cap=8, startable=3).free == 3
    assert _state(depth=20, active=2, cap=8, startable=0).free == 0
    assert _state(depth=20, active=2, cap=8, startable=12).free == 6


def test_a_queue_waiting_for_a_human_has_no_free_slot_whatever_its_cap():
    assert _state(depth=5, active=1, cap=8, held=True).free == 0


def test_a_run_that_is_not_a_delivery_keeps_its_assignment_to_itself():
    assert _state(depth=5, active=1, cap=8, exclusive=True).free == 0


def test_a_queue_whose_keys_are_all_in_flight_takes_no_share_of_the_drain():
    """A key-blocked assignment beside a healthy one: every slot goes to the
    healthy one instead of being planned for runs that cannot start."""
    blocked = _state(depth=20, active=3, cap=8, startable=0, waited=900)
    healthy = _state(depth=20, cap=8, waited=10)
    plan = dispatch.plan_promotions([blocked, healthy], capacity=6, max_assignments=1)
    assert plan == {healthy.assignment_id: 6}


def test_promotions_go_round_robin_over_agents_before_assignments():
    busy_agent, quiet_agent = uuid.uuid4(), uuid.uuid4()
    busy = [_state(agent=busy_agent, waited=300 - i) for i in range(3)]
    quiet = _state(agent=quiet_agent, waited=10)
    plan = dispatch.plan_promotions([*busy, quiet], capacity=6, max_assignments=20)
    # The quiet agent's one assignment gets as many slots as the busy agent's
    # three together: it is one of two agents, not one of four assignments.
    assert plan[quiet.assignment_id] == 3
    assert sum(plan[s.assignment_id] for s in busy) == 3
    assert all(plan[s.assignment_id] == 1 for s in busy)


def test_planning_never_exceeds_the_capacity_nor_an_assignment_s_free_slots():
    states = [_state(depth=3, cap=8), _state(depth=20, cap=2), _state(depth=20, active=7, cap=8)]
    plan = dispatch.plan_promotions(states, capacity=100, max_assignments=20)
    assert plan == {states[0].assignment_id: 3, states[1].assignment_id: 2, states[2].assignment_id: 1}
    assert sum(dispatch.plan_promotions(states, capacity=4, max_assignments=20).values()) == 4
    assert dispatch.plan_promotions(states, capacity=0, max_assignments=20) == {}


def test_held_and_full_queues_take_no_part_in_the_plan():
    held = _state(held=True, waited=900)
    full = _state(active=8, cap=8, waited=800)
    idle = _state(waited=5)
    plan = dispatch.plan_promotions([held, full, idle], capacity=10, max_assignments=1)
    assert plan == {idle.assignment_id: 5}


def test_the_longest_waiting_queues_are_planned_first_when_the_batch_is_bounded():
    states = [_state(waited=w) for w in (10, 500, 50, 900)]
    plan = dispatch.plan_promotions(states, capacity=10, max_assignments=2)
    assert set(plan) == {states[3].assignment_id, states[1].assignment_id}


def _queued(*keys: str | None) -> list[QueuedRun]:
    return [
        QueuedRun(run_id=uuid.uuid4(), created_at=_BASE + timedelta(seconds=i), ordering_key=key)
        for i, key in enumerate(keys)
    ]


def test_runs_start_oldest_first():
    queued = _queued(None, None, None, None)
    chosen, _ = dispatch.select_runs(queued, 2, busy_keys=set())
    assert chosen == queued[:2]


def test_runs_sharing_an_ordering_key_start_one_at_a_time_in_arrival_order():
    queued = _queued("a", "a", "b", None, "a")
    chosen, _ = dispatch.select_runs(queued, 5, busy_keys=set())
    assert chosen == [queued[0], queued[2], queued[3]]
    chosen, _ = dispatch.select_runs(queued, 5, busy_keys={"a"})
    assert chosen == [queued[2], queued[3]]


def test_a_backlog_alternates_the_newest_and_the_oldest_and_drops_nothing():
    queued = _queued(*[None] * 6)
    chosen, newest_next = dispatch.select_runs(queued, 4, busy_keys=set(), backlog=True, newest_turn=True)
    assert chosen == [queued[5], queued[0], queued[4], queued[1]]
    assert newest_next is True
    rest, _ = dispatch.select_runs(
        [run for run in queued if run not in chosen], 10, busy_keys=set(), backlog=True, newest_turn=newest_next
    )
    assert sorted([*chosen, *rest], key=lambda run: run.created_at) == queued


def test_a_backlog_keeps_an_ordering_key_s_order():
    queued = _queued("k", "k", "k", None)
    chosen, _ = dispatch.select_runs(queued, 4, busy_keys=set(), backlog=True, newest_turn=True)
    # The newest startable run is the unordered one; the key's head, never its tail.
    assert chosen == [queued[3], queued[0]]


def _select_runs_one_pick_at_a_time(queued, count, *, busy_keys, backlog, newest_turn):
    """The obvious form: recompute the candidates after every pick."""
    chosen, busy, left = [], set(busy_keys), list(queued)
    while len(chosen) < count:
        candidates = dispatch.startable_runs(left, busy)
        if not candidates:
            break
        run = candidates[-1] if backlog and newest_turn else candidates[0]
        chosen.append(run)
        left.remove(run)
        if run.ordering_key is not None:
            busy.add(run.ordering_key)
        if backlog:
            newest_turn = not newest_turn
    return chosen, newest_turn


def test_select_runs_picks_what_recomputing_after_every_pick_would():
    rng = random.Random(5026)  # noqa: S311 - a seeded, reproducible sample
    for _ in range(500):
        queued = _queued(*[rng.choice([None, "a", "b", "c", "d"]) for _ in range(rng.randint(0, 25))])
        arguments = {
            "busy_keys": set(rng.sample(["a", "b", "c", "d"], rng.randint(0, 2))),
            "backlog": rng.random() < 0.5,
            "newest_turn": rng.random() < 0.5,
        }
        count = rng.randint(0, 12)
        assert dispatch.select_runs(queued, count, **arguments) == _select_runs_one_pick_at_a_time(
            queued, count, **arguments
        )


def test_a_throttled_cap_is_halved_and_never_zero():
    assert dispatch.throttled_cap(8) == 4
    assert dispatch.throttled_cap(3) == 1
    assert dispatch.throttled_cap(1) == 1


# ---------------------------------------------------------------------------
# 429 back-off
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.reads = 0

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        self.ttls[key] = ex
        return True

    async def mget(self, keys):
        self.reads += 1
        return [self.values.get(key) for key in keys]


def _throttle_key(assignment_id: uuid.UUID) -> str:
    return f"assignment_dispatch:throttled:{assignment_id}"


@pytest.fixture
def fake_redis(monkeypatch):
    redis = _FakeRedis()

    async def _redis():
        return redis

    monkeypatch.setattr(dispatch, "_redis", _redis)
    monkeypatch.setattr(dispatch, "_throttle_cache", {})
    monkeypatch.setattr(dispatch.settings, "assignment_dispatch_rate_limit_backoff_seconds", 60)
    return redis


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["llm", "integration"])
async def test_a_429_backs_off_only_the_assignment_whose_run_met_it(fake_redis, source):
    first, second = uuid.uuid4(), uuid.uuid4()
    with dispatch.dispatch_scope(second):
        await dispatch.note_rate_limited(source)
    assert await dispatch.read_throttles([first, second]) == {second}
    assert fake_redis.ttls == {_throttle_key(second): 60}


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["llm", "integration"])
async def test_a_429_met_outside_an_assignment_run_throttles_nothing(fake_redis, source):
    """A chat, an evaluation or an embedding hitting a 429 - on a user's own key
    as often as not - must not slow every assignment of the platform."""
    await dispatch.note_rate_limited(source)
    assert fake_redis.values == {}


@pytest.mark.asyncio
async def test_the_back_off_window_is_fixed_from_the_first_429(fake_redis):
    """A run retrying through a long 429 streak must not keep its assignment
    throttled for as long as the streak lasts."""
    assignment = uuid.uuid4()
    with dispatch.dispatch_scope(assignment):
        await dispatch.note_rate_limited("llm")
        fake_redis.ttls[_throttle_key(assignment)] = 12  # the window has partly run
        await dispatch.note_rate_limited("llm")
    assert fake_redis.ttls == {_throttle_key(assignment): 12}


@pytest.mark.asyncio
async def test_the_back_off_is_off_at_zero_seconds(fake_redis, monkeypatch):
    monkeypatch.setattr(dispatch.settings, "assignment_dispatch_rate_limit_backoff_seconds", 0)
    with dispatch.dispatch_scope(uuid.uuid4()):
        await dispatch.note_rate_limited("llm")
    assert fake_redis.values == {}


@pytest.mark.asyncio
async def test_without_redis_nothing_backs_off_and_nothing_raises(monkeypatch):
    # The autouse conftest fixture makes ``_redis`` raise, as a deployment
    # without Redis would.
    monkeypatch.setattr(dispatch, "_throttle_cache", {})
    with dispatch.dispatch_scope(uuid.uuid4()):
        await dispatch.note_rate_limited("llm")
    assert await dispatch.read_throttles([uuid.uuid4()]) == set()
    assert await dispatch.is_throttled(uuid.uuid4()) is False


@pytest.mark.asyncio
async def test_a_delivery_decision_reads_its_assignment_s_back_off_at_most_every_few_seconds(fake_redis):
    """A poll page dispatching many events of one assignment reads the back-off
    once, not once per event."""
    assignment = uuid.uuid4()
    fake_redis.values[_throttle_key(assignment)] = "1"
    assert [await dispatch.is_throttled(assignment) for _ in range(5)] == [True] * 5
    assert fake_redis.reads == 1

    dispatch._throttle_cache[assignment] = (0.0, True)  # the cached answer expired
    del fake_redis.values[_throttle_key(assignment)]
    assert await dispatch.is_throttled(assignment) is False
    assert fake_redis.reads == 2


@pytest.mark.asyncio
async def test_a_429_is_seen_by_the_next_decision_of_its_process_without_a_read(fake_redis):
    assignment = uuid.uuid4()
    assert await dispatch.is_throttled(assignment) is False
    with dispatch.dispatch_scope(assignment):
        await dispatch.note_rate_limited("integration")
    assert await dispatch.is_throttled(assignment) is True
    assert fake_redis.reads == 1


class _RateLimitedError(Exception):
    status_code = 429


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_RateLimitedError(), True),
        (type("E", (Exception,), {"response": type("R", (), {"status_code": 429})()})(), True),
        (type("E", (Exception,), {"response": {"Error": {"Code": "ThrottlingException"}}})(), True),
        (type("E", (Exception,), {"status_code": 529})(), False),
        (ValueError("429 in the message only"), False),
    ],
)
def test_what_counts_as_a_model_provider_rate_limit(exc, expected):
    from app.llm.base import is_rate_limit_error

    assert is_rate_limit_error(exc) is expected


@pytest.mark.asyncio
async def test_retry_llm_call_reports_a_429_and_still_retries(monkeypatch):
    from app.llm.base import retry_llm_call

    noted: list[str] = []

    async def _note(source):
        noted.append(source)

    monkeypatch.setattr(dispatch, "note_rate_limited", _note)
    calls = 0

    async def _call():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _RateLimitedError()
        return "ok"

    assert await retry_llm_call(_call, is_retryable=lambda _e: True, base_delay=0, max_delay=0) == "ok"
    assert noted == ["llm"]


@pytest.mark.asyncio
async def test_safe_request_reports_each_429_and_still_retries(monkeypatch):
    import httpx

    from app.integrations import http as http_mod

    noted: list[str] = []

    async def _note(source):
        noted.append(source)

    monkeypatch.setattr(dispatch, "note_rate_limited", _note)
    monkeypatch.setattr(http_mod, "_RATE_LIMIT_BASE_DELAY", 0)
    answers = iter([429, 200])

    def _handler(_request):
        return httpx.Response(next(answers), headers={"Retry-After": "0"}, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        resp, err = await http_mod.safe_request(client, "GET", "https://api.example.test/x", "example", "read")
    assert err is None and resp.status_code == 200
    assert noted == ["integration"]


# ---------------------------------------------------------------------------
# Load simulation
# ---------------------------------------------------------------------------


@dataclass
class _Delivery:
    assignment: str
    arrived: float
    duration: float
    key: str | None = None
    run_id: uuid.UUID = field(default_factory=uuid.uuid4)
    started: float | None = None

    @property
    def lag(self) -> float:
        assert self.started is not None
        return self.started - self.arrived


def _simulate(
    deliveries: list[_Delivery],
    *,
    policy: str,
    workers: int = 10,
    cap: int = 10,
    dispatch_seconds: float = 2.0,
    tick_seconds: float = 60.0,
    backlog_seconds: float = 300.0,
    agents: dict[str, str] | None = None,
    start_at: float = 0.0,
    max_depth: int = 20,
) -> list[tuple[float, _Delivery]]:
    """Run ``deliveries`` through one dispatch policy; returns the starts in order.

    ``event``: the drain (``plan_promotions`` + ``select_runs``) runs on every
    arrival and every finished run; a promoted run starts ``dispatch_seconds``
    later (reconciler wake + claim) and holds its assignment's slot from the
    moment it is promoted. ``poll``: the same drain, but deliveries wait at the
    provider until a ``tick_seconds`` poll reads them in order through
    ``trigger_or_queue_assignment``'s rule - start when nothing is queued and
    a slot is free, queue up to ``max_depth``, and on a full queue stop and
    leave the rest for the next poll. ``tick``: the dispatch this replaced -
    on each poll, an idle assignment starts its oldest delivery and a busy one
    starts nothing. ``start_at``: deliveries that arrived earlier (an outage)
    are all waiting when dispatch begins.
    """
    names = sorted({d.assignment for d in deliveries})
    ids = {name: uuid.uuid5(uuid.NAMESPACE_URL, f"assignment/{name}") for name in names}
    by_id = {value: key for key, value in ids.items()}
    agent_of = {name: uuid.uuid5(uuid.NAMESPACE_URL, f"agent/{(agents or {}).get(name, name)}") for name in names}
    by_run = {d.run_id: d for d in deliveries}
    queued: dict[str, list[_Delivery]] = {name: [] for name in names}
    active: dict[str, list[_Delivery]] = {name: [] for name in names}
    turns: dict[str, bool] = {}
    starts: list[tuple[float, _Delivery]] = []
    events: list[tuple[float, int, str, _Delivery | None]] = []
    seq = 0

    def push(at: float, kind: str, delivery: _Delivery | None = None) -> None:
        nonlocal seq
        seq += 1
        heapq.heappush(events, (at, seq, kind, delivery))

    def start(now: float, delivery: _Delivery) -> None:
        queued[delivery.assignment].remove(delivery)
        active[delivery.assignment].append(delivery)
        delivery.started = now + dispatch_seconds
        starts.append((now, delivery))
        push(delivery.started + delivery.duration, "finish", delivery)

    def drain(now: float) -> None:
        states = [
            QueueState(
                assignment_id=ids[name],
                agent_id=agent_of[name],
                depth=len(queued[name]),
                oldest_at=_BASE + timedelta(seconds=queued[name][0].arrived),
                active=len(active[name]),
                cap=cap,
            )
            for name in names
            if queued[name]
        ]
        capacity = workers - sum(len(runs) for runs in active.values())
        for assignment_id, count in dispatch.plan_promotions(states, capacity=capacity, max_assignments=20).items():
            name = by_id[assignment_id]
            candidates = [QueuedRun(d.run_id, _BASE + timedelta(seconds=d.arrived), d.key) for d in queued[name]]
            backlog = now - queued[name][0].arrived > backlog_seconds
            chosen, turn = dispatch.select_runs(
                candidates,
                count,
                busy_keys={d.key for d in active[name] if d.key},
                backlog=backlog,
                newest_turn=turns.get(name, False),
            )
            turns[name] = turn if backlog else False
            for run in chosen:
                start(now, by_run[run.run_id])

    def tick(now: float) -> None:
        for name in names:
            if queued[name] and not active[name] and sum(len(r) for r in active.values()) < workers:
                start(now, queued[name][0])

    def poll(now: float) -> None:
        for name in names:
            waiting = provider[name]
            while waiting and waiting[0].arrived <= now:
                if not queued[name] and len(active[name]) < cap:
                    queued[name].append(waiting.pop(0))
                    start(now, queued[name][0])
                elif len(queued[name]) < max_depth:
                    queued[name].append(waiting.pop(0))
                else:
                    break
        drain(now)

    provider: dict[str, list[_Delivery]] = {name: [] for name in names}
    intake = provider if policy == "poll" else queued
    last_arrival = max(d.arrived for d in deliveries)
    for delivery in deliveries:
        if delivery.arrived < start_at:
            intake[delivery.assignment].append(delivery)
        else:
            push(delivery.arrived, "arrive", delivery)
    for name in names:
        intake[name].sort(key=lambda d: d.arrived)
    push(start_at, "wake" if policy == "event" else "tick")

    while events:
        now, _seq, kind, delivery = heapq.heappop(events)
        if kind == "arrive":
            intake[delivery.assignment].append(delivery)
        elif kind == "finish":
            active[delivery.assignment].remove(delivery)
        if kind == "tick":
            (tick if policy == "tick" else poll)(now)
            if now < last_arrival or any(queued.values()) or any(provider.values()):
                push(now + tick_seconds, "tick")
        elif policy == "event" or (policy == "poll" and kind == "finish"):
            drain(now)
    assert all(not runs for runs in [*queued.values(), *provider.values()]), "every delivery starts"
    return starts


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, -(-95 * len(ordered) // 100) - 1)]


def _stream(assignment: str, per_minute: float, minutes: float, rng: random.Random, *, keys=None, offset=0.0):
    gap = 60.0 / per_minute
    count = int(minutes * per_minute)
    return [
        _Delivery(
            assignment=assignment,
            arrived=offset + i * gap,
            duration=rng.uniform(6, 22),
            key=keys[i % len(keys)] if keys else None,
        )
        for i in range(count)
    ]


def _max_in_flight(starts: list[tuple[float, _Delivery]], assignment: str | None = None) -> int:
    edges = sorted(
        edge
        for _promoted, d in starts
        if assignment is None or d.assignment == assignment
        for edge in ((d.started, 1), (d.started + d.duration, -1))
    )
    peak = level = 0
    for _at, step in edges:
        level += step
        peak = max(peak, level)
    return peak


def _assert_ordering_keys_respected(deliveries: list[_Delivery]) -> None:
    by_key: dict[tuple[str, str], list[_Delivery]] = {}
    for d in deliveries:
        if d.key is not None:
            by_key.setdefault((d.assignment, d.key), []).append(d)
    for runs in by_key.values():
        runs.sort(key=lambda d: d.arrived)
        for earlier, later in itertools.pairwise(runs):
            assert later.started >= earlier.started + earlier.duration, "a key's runs never overlap, in order"


def test_two_hundred_queued_deliveries_over_three_assignments_start_once_each_in_fair_turns():
    """Acceptance: 200 deliveries left queued by an outage, over 3 assignments,
    all start - more than one at a time, none twice, none dropped - and the
    three assignments take turns instead of one draining before the others."""
    rng = random.Random(5015)  # noqa: S311 - a seeded, reproducible load
    counts = {"alerts": 120, "mail": 50, "tickets": 30}
    deliveries: list[_Delivery] = []
    for index in range(max(counts.values())):
        for name, count in counts.items():
            if index < count:
                deliveries.append(_Delivery(name, arrived=-3600 + len(deliveries), duration=rng.uniform(6, 22)))

    starts = _simulate(deliveries, policy="event")

    assert len(starts) == 200
    assert len({d.run_id for _at, d in starts}) == 200
    assert _max_in_flight(starts) == 10, "the worker pool is used, not one run at a time"
    assert all(_max_in_flight(starts, name) > 1 for name in counts)
    # After every drain, while every assignment still has a backlog, none is
    # ahead of another by more than a couple of runs (a drain promotes its plan
    # one assignment after the other, so the check is per drain).
    taken = dict.fromkeys(counts, 0)
    for index, (at, d) in enumerate(starts):
        taken[d.assignment] += 1
        drain_ended = index + 1 == len(starts) or starts[index + 1][0] != at
        if drain_ended and all(taken[name] < counts[name] for name in counts):
            assert max(taken.values()) - min(taken.values()) <= 2, taken


def test_a_thirty_per_minute_stream_keeps_p95_lag_under_a_minute_without_starving_the_rest():
    """Acceptance: 30 deliveries a minute on one assignment plus background
    load keep the p95 dispatch lag under 60 s, and the quieter assignments
    (one with ordering keys) are never starved. The 60 s poll tick this
    replaced falls hours behind on the same load."""
    rng = random.Random(5015)  # noqa: S311 - a seeded, reproducible load

    def load() -> list[_Delivery]:
        return [
            *_stream("hot", 30, 30, rng),
            *_stream("digest", 3, 30, rng, offset=7),
            *_stream("threads", 3, 30, rng, keys=["t1", "t2", "t3"], offset=13),
        ]

    after = load()
    _simulate(after, policy="event")
    before = [_Delivery(d.assignment, d.arrived, d.duration, d.key) for d in after]
    _simulate(before, policy="tick")

    assert _p95([d.lag for d in after]) < 60
    for name in ("hot", "digest", "threads"):
        assert _p95([d.lag for d in after if d.assignment == name]) < 60, name
    assert max(d.lag for d in after if d.assignment != "hot") < 60, "no starvation"
    _assert_ordering_keys_respected(after)
    assert _p95([d.lag for d in before]) > 3600, "one run per assignment per minute cannot keep up"


def test_a_backlog_interleaves_fresh_deliveries_with_the_oldest_and_drops_nothing():
    """After an outage, fresh deliveries start within a minute instead of
    waiting behind the whole backlog, while every other pick still takes the
    oldest delivery, and every delivery starts once."""
    rng = random.Random(5015)  # noqa: S311 - a seeded, reproducible load
    backlog = [_Delivery("mail", arrived=-7200 + i * 10, duration=rng.uniform(6, 22)) for i in range(700)]
    interleaved = [*backlog, *_stream("mail", 4, 20, rng)]
    fifo = [_Delivery(d.assignment, d.arrived, d.duration) for d in interleaved]

    starts = _simulate(interleaved, policy="event", workers=4, cap=4)
    _simulate(fifo, policy="event", workers=4, cap=4, backlog_seconds=float("inf"))

    assert len({d.run_id for _at, d in starts}) == len(interleaved)
    assert backlog[0] in [d for _at, d in starts[:2]], "the oldest delivery still goes first"
    fresh = [d.lag for d in interleaved if d.arrived >= 0]
    fresh_fifo = [d.lag for d in fifo if d.arrived >= 0]
    assert _p95(fresh) < 60
    assert _p95(fresh_fifo) > 1800, "oldest first alone leaves fresh work behind the whole backlog"


def _caught_up_at(deliveries: list[_Delivery], *, age: float, step: float = 5.0) -> float:
    """The first moment from which the oldest delivery still waiting stays
    under ``age`` seconds old."""
    end = max(d.started for d in deliveries)
    caught_up, now = 0.0, 0.0
    while now <= end:
        waiting = [now - d.arrived for d in deliveries if d.arrived <= now < d.started]
        if waiting and max(waiting) >= age:
            caught_up = now + step
        now += step
    return caught_up


def test_the_incident_backlog_is_caught_up_within_fifteen_minutes():
    """The incident: a polled Mail Desk assignment 2h20 behind at about
    a mail a minute. The poller now reads until the queue is full on every
    tick and the drain runs the queue in parallel, so the oldest waiting mail
    is under five minutes old within fifteen minutes; one run per tick kept
    the lag where it was until the inflow stopped."""
    rng = random.Random(5015)  # noqa: S311 - a seeded, reproducible load
    after = _stream("mail", 1, 140 + 30, rng, offset=-140 * 60)
    before = [_Delivery(d.assignment, d.arrived, d.duration) for d in after]

    _simulate(after, policy="poll")
    _simulate(before, policy="tick")

    assert _caught_up_at(after, age=300) <= 15 * 60
    assert max(d.lag for d in after if d.arrived >= 15 * 60) < 60
    assert _caught_up_at(before, age=300) > 2 * 3600
