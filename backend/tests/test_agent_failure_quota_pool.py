"""Tests for the per-agent failure quota pool (``AGENT_FAILURE_QUOTA_POOL``).

An agentic execution is metered before the agent runs, so a run that ends in
failure has already spent one action of the user's action quota.
``refund_agentic_usage_for_failure`` credits back a *bounded* number of those
failures per (user, agent, pool window) — the pool — and lets every failure
past it spend its action, so a permanently broken agent cannot run for free.
Sized in env only, on by default.
"""

from __future__ import annotations

import base64
import contextlib
import inspect
import json
import pathlib
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import app.api.v1.extension as extension_mod
import app.services.agent_chat as agent_chat_mod
import app.services.agent_failure_pool as pool_mod
import app.services.assignment_engine as assignment_engine_mod
import app.services.support_codes as support_codes_mod
from app.llm.router import is_provider_unavailable_error
from app.services import quota as quota_module
from app.services.agent_failure_pool import (
    POOL_RECHARGE_KIND,
    SUPPORT_CODE_PREFIX,
    SupportCodeError,
    _window_from_anchor,
    resolve_pool_window,
    verify_support_code,
)
from app.services.quota import (
    _current_period_key,
    _failure_pool_key,
    _free_refund_key,
    _global_usage_key,
    _usage_key,
    agent_failure_pool_size,
    increment_agentic_usage,
    refund_agentic_usage_by_receipt,
    refund_agentic_usage_for_failure,
    refund_agentic_usage_for_provider_outage,
)
from tests.quota_redis_scripts import RedisHashes, run_quota_script


class _FakeRedis(RedisHashes):
    """Minimal counter store — same shape as ``test_quota_refund``'s."""

    def __init__(self):
        self.store: dict[str, int] = {}
        self.hashes: dict[str, dict[str, int]] = {}

    async def get(self, key):
        val = self.store.get(key)
        return None if val is None else str(val).encode()

    async def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def decr(self, key):
        self.store[key] = self.store.get(key, 0) - 1
        return self.store[key]

    async def incrby(self, key, amount):
        self.store[key] = self.store.get(key, 0) + amount
        return self.store[key]

    async def expire(self, key, ttl):
        return True

    async def set(self, key, value, ex=None, **kwargs):
        self.store[key] = value
        return True

    async def getdel(self, key):
        value = self.store.pop(key, None)
        if value is None:
            return None
        return value.encode() if isinstance(value, str) else str(value).encode()

    async def delete(self, key):
        self.store.pop(key, None)
        return 1

    async def eval(self, script, numkeys, *keys_and_args):
        return run_quota_script(self.store, self.hashes, script, keys_and_args)


def _enable(monkeypatch, pool: int = 3) -> None:
    monkeypatch.setattr(
        quota_module.settings, "agent_failure_quota_refund_enabled", True
    )
    monkeypatch.setattr(quota_module.settings, "agent_failure_quota_pool", pool)


async def _window_key() -> str:
    """The pool's current window key — a YEAR, not the agentic quota period.

    Resolved through the real ``resolve_pool_window`` (with no session, which
    degrades to the calendar year) rather than hardcoded, so these tests pin
    the pool's own clock without pinning today's date.
    """
    from app.services.agent_failure_pool import resolve_pool_window

    return (await resolve_pool_window(None)).key


def _fake_redis(monkeypatch) -> _FakeRedis:
    redis = _FakeRedis()
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    return redis


# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------


def test_pool_size_zero_when_disabled(monkeypatch):
    """The flag is the master switch: a sized pool with the flag off is off."""
    monkeypatch.setattr(
        quota_module.settings, "agent_failure_quota_refund_enabled", False
    )
    monkeypatch.setattr(quota_module.settings, "agent_failure_quota_pool", 3)
    assert agent_failure_pool_size() == 0


def test_pool_size_never_negative(monkeypatch):
    """A misconfigured negative pool reads as closed, not as unlimited."""
    _enable(monkeypatch, pool=-5)
    assert agent_failure_pool_size() == 0


async def test_disabled_feature_keeps_the_charge(monkeypatch):
    """With the flag off, a failed run spends its action like a successful one."""
    redis = _fake_redis(monkeypatch)
    monkeypatch.setattr(
        quota_module.settings, "agent_failure_quota_refund_enabled", False
    )
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()

    await increment_agentic_usage(user_id, agent_id=agent_id)
    assert await refund_agentic_usage_for_failure(user_id, agent_id) is False

    period_key = _current_period_key("monthly")
    assert redis.store[_usage_key(user_id, "agentic", None, period_key)] == 1
    assert redis.store[_global_usage_key("agentic", period_key)] == 1
    # No pool counter is even created while the feature is off.
    assert _failure_pool_key(user_id, agent_id, await _window_key()) not in redis.store


# ---------------------------------------------------------------------------
# Refund inside the pool
# ---------------------------------------------------------------------------


async def test_failed_run_inside_pool_is_refunded(monkeypatch):
    """A charge + a failure inside the pool nets every counter back to zero."""
    redis = _fake_redis(monkeypatch)
    _enable(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()

    await increment_agentic_usage(user_id, agent_id=agent_id)
    assert await refund_agentic_usage_for_failure(user_id, agent_id) is True

    period_key = _current_period_key("monthly")
    assert redis.store[_usage_key(user_id, "agentic", None, period_key)] == 0
    assert redis.store[_usage_key(user_id, "agentic", None, period_key, agent_id)] == 0
    assert redis.store[_global_usage_key("agentic", period_key)] == 0
    assert redis.store[_failure_pool_key(user_id, agent_id, await _window_key())] == 1


async def test_pool_bounds_the_refunds(monkeypatch):
    """Failures 1-3 are forgiven; the 4th is charged and the pool holds at 3."""
    redis = _fake_redis(monkeypatch)
    _enable(monkeypatch, pool=3)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()

    granted = []
    for _ in range(4):
        await increment_agentic_usage(user_id, agent_id=agent_id)
        granted.append(await refund_agentic_usage_for_failure(user_id, agent_id))

    assert granted == [True, True, True, False]

    period_key = _current_period_key("monthly")
    # Exactly the one unrefunded failure remains charged.
    assert redis.store[_usage_key(user_id, "agentic", None, period_key)] == 1
    assert redis.store[_global_usage_key("agentic", period_key)] == 1
    # The refused claim is handed back, so the counter never drifts above the
    # pool size — otherwise the durable high-water mark persisted by the flush
    # would overstate what the window actually consumed.
    assert redis.store[_failure_pool_key(user_id, agent_id, await _window_key())] == 3


async def test_pool_is_per_agent(monkeypatch):
    """One agent exhausting its pool does not spend another agent's."""
    redis = _fake_redis(monkeypatch)
    _enable(monkeypatch, pool=1)
    user_id = uuid.uuid4()
    agent_a, agent_b = uuid.uuid4(), uuid.uuid4()

    assert await refund_agentic_usage_for_failure(user_id, agent_a) is True
    assert await refund_agentic_usage_for_failure(user_id, agent_a) is False
    # Agent B still has its own full pool.
    assert await refund_agentic_usage_for_failure(user_id, agent_b) is True

    window_key = await _window_key()
    assert redis.store[_failure_pool_key(user_id, agent_a, window_key)] == 1
    assert redis.store[_failure_pool_key(user_id, agent_b, window_key)] == 1


async def test_pool_is_per_user(monkeypatch):
    """A shared (company-managed) agent: one user cannot eat another's pool."""
    redis = _fake_redis(monkeypatch)
    _enable(monkeypatch, pool=1)
    agent_id = uuid.uuid4()
    user_a, user_b = uuid.uuid4(), uuid.uuid4()

    assert await refund_agentic_usage_for_failure(user_a, agent_id) is True
    assert await refund_agentic_usage_for_failure(user_a, agent_id) is False
    assert await refund_agentic_usage_for_failure(user_b, agent_id) is True

    window_key = await _window_key()
    assert redis.store[_failure_pool_key(user_a, agent_id, window_key)] == 1
    assert redis.store[_failure_pool_key(user_b, agent_id, window_key)] == 1


async def test_refund_never_underflows_other_usage(monkeypatch):
    """A refund reverses one run, not the user's history: two charges then one
    credited-back failure leaves exactly one action consumed."""
    redis = _fake_redis(monkeypatch)
    _enable(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()

    await increment_agentic_usage(user_id, agent_id=agent_id)
    await increment_agentic_usage(user_id, agent_id=agent_id)
    assert await refund_agentic_usage_for_failure(user_id, agent_id) is True

    period_key = _current_period_key("monthly")
    assert redis.store[_usage_key(user_id, "agentic", None, period_key)] == 1
    assert redis.store[_global_usage_key("agentic", period_key)] == 1


async def test_never_raises_when_redis_is_down(monkeypatch):
    """Every call site is already handling a failure — the refund must not add
    an exception of its own."""
    _enable(monkeypatch)
    monkeypatch.setattr(
        quota_module,
        "redis_client",
        AsyncMock(side_effect=RuntimeError("redis unreachable")),
    )
    assert await refund_agentic_usage_for_failure(uuid.uuid4(), uuid.uuid4()) is False


async def test_pool_key_is_durable_but_distinct_from_usage(monkeypatch):
    """The pool counter deliberately rides ``quota:usage:*`` — that is what
    ``flush_quota_usage`` persists, and a year-long window cannot live in Redis
    alone — while carrying its own usage type so no quota read can mistake it
    for action-quota usage."""
    _enable(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    key = _failure_pool_key(user_id, agent_id, "2026-08-25")

    # Flushable: same 5-segment shape flush_quota_usage parses.
    assert key.startswith(quota_module._USAGE_PREFIX)
    parts = key.removeprefix(quota_module._USAGE_PREFIX).split(":")
    assert parts == [
        str(user_id),
        quota_module._POOL_USAGE_TYPE,
        "_total",
        "2026-08-25",
        str(agent_id),
    ]
    # ...and invisible to the quota reads, which enumerate their types.
    assert quota_module._POOL_USAGE_TYPE not in quota_module._QUOTA_TYPES
    # The window key must fit quota_usage.period (String(10)).
    assert len("2026-08-25") <= 10


# ---------------------------------------------------------------------------
# Wiring — source-level pins
# ---------------------------------------------------------------------------
# The three metered failure paths (chat turn, assignment run, extension task)
# all sit behind a live DB + LLM provider mid-orchestrator, so they are pinned
# at source level — the established pattern in this suite for orchestrator
# internals (see the memory-scope and LLM-resolution-refund pins in
# test_agent_chat_smoke.py).

_CHAT_SRC = inspect.getsource(agent_chat_mod.run_agent_chat)


def test_every_failed_chat_turn_path_refunds():
    """Each chat failure path that marks the turn failed must also offer it to
    the failure pool — otherwise a context overflow refunds but an HTTP 413
    silently doesn't."""
    lines = _CHAT_SRC.splitlines()
    fail_lines = [
        i for i, ln in enumerate(lines) if "await _fail_chat_parent_run(" in ln
    ]
    assert fail_lines, "the chat failure paths moved — re-pin this test"
    for i in fail_lines:
        # A comment explaining WHICH budget pays can sit between the two calls,
        # so this is a proximity pin, not an adjacency one.
        window = "\n".join(lines[i : i + 6])
        assert "await _refund_failed_turn(" in window, (
            f"the failure path at line {i + 1} of run_agent_chat marks the turn "
            "failed without offering it to the agent failure pool"
        )


def test_chat_refund_only_reverses_what_this_turn_metered():
    """A nested handover, an evaluation run and an unmetered turn must not be
    refunded — the pool slot belongs to whoever was actually charged."""
    assert "if _turn_refunded or not _quota_metered:" in _CHAT_SRC


def test_cancelled_chat_turn_is_not_refunded():
    """Awaiting inside a cancelled task raises again, which would swallow the
    original exception — and a cancellation is not a failure of the agent."""
    assert "if isinstance(exc, Exception):" in _CHAT_SRC


def test_failed_assignment_run_refunds_only_when_charged():
    """The assignment failure handler must refund, and only for a run whose
    quota was actually charged (a builtin-exempt or unmetered run has nothing
    to give back)."""
    src = inspect.getsource(assignment_engine_mod.execute_assignment_run)
    assert 'run.status = "failed"' in src
    assert "refund_agentic_usage_for_failure(" in src
    refund_at = src.index("refund_agentic_usage_for_failure(")
    gate_at = src.index('_quota_trace_meta.get("quota_actions_consumed")')
    assert gate_at < refund_at, "the refund must sit behind the charged gate"


def test_failed_extension_task_refunds():
    """The extension endpoint meters the task itself (``meter_quota=False`` on
    the inner chat call), so it must do its own failure refund."""
    src = inspect.getsource(extension_mod)
    assert "refund_agentic_usage_for_failure(" in src


# ---------------------------------------------------------------------------
# Renewal window — one year, on the license anniversary
# ---------------------------------------------------------------------------


def test_window_sits_on_the_anniversary_not_the_calendar_year():
    """A licence starting mid-year renews mid-year: the pool follows the
    commercial cycle, not January."""
    anchor = datetime(2024, 3, 14, 9, 30, tzinfo=UTC)
    window = _window_from_anchor(anchor, datetime(2026, 8, 25, tzinfo=UTC), "xtm_license")
    assert window.start == datetime(2026, 3, 14, 9, 30, tzinfo=UTC)
    assert window.end == datetime(2027, 3, 14, 9, 30, tzinfo=UTC)
    assert window.key == "2026-03-14"


def test_window_before_the_first_anniversary_is_the_anchor_year():
    """Between the licence start and its first anniversary there is exactly one
    window, the one that opened at the start."""
    anchor = datetime(2026, 3, 14, tzinfo=UTC)
    window = _window_from_anchor(anchor, datetime(2026, 8, 25, tzinfo=UTC), "xtm_license")
    assert window.start == anchor
    assert window.end == datetime(2027, 3, 14, tzinfo=UTC)


def test_window_is_contiguous_across_the_anniversary():
    """The instant before an anniversary and the instant after must land in
    consecutive windows — no gap where failures are charged twice, no overlap
    where a pool is spent from two windows at once."""
    anchor = datetime(2024, 6, 1, tzinfo=UTC)
    before = _window_from_anchor(anchor, datetime(2026, 5, 31, 23, 59, tzinfo=UTC), "x")
    after = _window_from_anchor(anchor, datetime(2026, 6, 1, 0, 1, tzinfo=UTC), "x")
    assert before.end == after.start
    assert before.key != after.key


def test_leap_day_anchor_survives_a_non_leap_year():
    """A licence issued on 29 February has no anniversary three years in four;
    it falls back to the 28th rather than raising."""
    anchor = datetime(2024, 2, 29, tzinfo=UTC)
    window = _window_from_anchor(anchor, datetime(2026, 8, 25, tzinfo=UTC), "xtm_license")
    assert window.start == datetime(2026, 2, 28, tzinfo=UTC)
    assert window.end == datetime(2027, 2, 28, tzinfo=UTC)


def test_anchor_in_the_future_yields_the_anchor_window():
    """Clock skew (or a licence that has not started) must not produce a
    window that has not begun."""
    anchor = datetime(2027, 1, 1, tzinfo=UTC)
    window = _window_from_anchor(anchor, datetime(2026, 8, 25, tzinfo=UTC), "xtm_license")
    assert window.start == anchor
    assert window.end == datetime(2028, 1, 1, tzinfo=UTC)


async def test_window_without_a_session_degrades_to_the_calendar_year():
    """The refund path can run without a usable session; the window must still
    be a stable, annual key rather than an error."""
    window = await resolve_pool_window(None)
    assert window.source == "calendar"
    assert window.key.endswith("-01-01")


# ---------------------------------------------------------------------------
# Durable baseline — a year-long pool cannot live in Redis alone
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value


class _BaselineDB:
    """Session stub answering the two statements ``_load_pool_baseline`` runs:
    the try of the pool-clear lock, then the durable row (``queries`` counts
    those reads). A reset or a recharge holds that lock for the first
    *clearing* tries, and *value* is the row it leaves behind. ``rollbacks``
    counts the transactions the claim gave back while it waited."""

    def __init__(self, value, *, clearing: int = 0):
        self._value = value
        self._clearing = clearing
        self.tries = 0
        self.queries = 0
        self.rollbacks = 0
        self.locks: list[dict] = []

    async def execute(self, stmt, *args, **_kwargs):
        if "pg_try_advisory_xact_lock_shared" in str(stmt):
            self.locks.append(dict(args[0]) if args else {})
            self.tries += 1
            return _FakeResult(self.tries > self._clearing)
        self.queries += 1
        return _FakeResult(self._value)

    async def rollback(self):
        self.rollbacks += 1


async def test_lost_redis_counter_is_rebuilt_from_the_durable_row(monkeypatch):
    """Redis losing the pool counter mid-window must NOT refill the pool: the
    flushed high-water mark is folded back in before the claim is judged."""
    redis = _fake_redis(monkeypatch)
    _enable(monkeypatch, pool=3)
    monkeypatch.setattr(
        "app.services.agent_failure_pool.resolve_pool_window",
        AsyncMock(return_value=_window_from_anchor(
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 8, 25, tzinfo=UTC), "x"
        )),
    )
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db = _BaselineDB(3)  # three failures already forgiven, per the DB

    granted = await quota_module._claim_failure_pool_slot_and_refund(
        redis, user_id, agent_id, db, "monthly", 3, "lost cache"
    )

    assert granted is False
    assert db.queries == 1
    # The counter now reflects the durable truth, not the empty cache.
    assert redis.store[_failure_pool_key(user_id, agent_id, "2026-01-01")] == 3


async def test_durable_row_is_only_read_when_the_cache_is_cold(monkeypatch):
    """The baseline read is a recovery path, not a per-failure DB hit: only the
    first claim of a window (or a lost counter) touches the durable row."""
    _fake_redis(monkeypatch)
    _enable(monkeypatch, pool=3)
    baseline = AsyncMock(return_value=0)
    monkeypatch.setattr(quota_module, "_load_pool_baseline", baseline)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()

    for _ in range(3):
        assert await refund_agentic_usage_for_failure(user_id, agent_id) is True

    assert baseline.await_count == 1


def _cold_claim_env(monkeypatch) -> _FakeRedis:
    redis = _fake_redis(monkeypatch)
    _enable(monkeypatch, pool=3)
    monkeypatch.setattr(quota_module, "_POOL_CLEAR_POLL_SECONDS", 0.001)
    monkeypatch.setattr(
        "app.services.agent_failure_pool.resolve_pool_window",
        AsyncMock(return_value=_window_from_anchor(
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 8, 25, tzinfo=UTC), "x"
        )),
    )
    monkeypatch.setattr(
        quota_module,
        "_resolve_agentic_periods",
        AsyncMock(return_value=("monthly", set())),
    )
    return redis


async def test_a_cold_claim_waits_out_a_pool_clear_that_rolls_back(monkeypatch):
    """A reset or a recharge deletes the durable row in a transaction that is
    still open while it sweeps the keys, and that transaction may yet roll
    back: the pool is then as spent as the row says. A claim finding its key
    swept waits for the clear to end, then reads the row it left - counting
    from zero here would have warmed a counter the restored row never reaches
    again, and granted refunds past a spent pool."""
    redis = _cold_claim_env(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db = _BaselineDB(3, clearing=3)  # rolled back: the row still says "spent"

    report: dict = {}
    granted = await quota_module._claim_failure_pool_slot_and_refund(
        redis, user_id, agent_id, db, "monthly", 3, "reset rolled back",
        outcome_out=report,
    )

    assert granted is False
    assert report["outcome"] == quota_module.REFUND_OUTCOME_POOL_EXHAUSTED
    assert (db.tries, db.rollbacks, db.queries) == (4, 3, 1)
    assert redis.store[_failure_pool_key(user_id, agent_id, "2026-01-01")] == 3


async def test_a_cold_claim_waits_out_a_pool_clear_that_commits(monkeypatch):
    """The same wait when the clear commits: the row is gone, the pool is
    whole again, and the claim is the first of the fresh pool."""
    redis = _cold_claim_env(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db = _BaselineDB(0, clearing=3)  # committed: no row left

    granted = await quota_module._claim_failure_pool_slot_and_refund(
        redis, user_id, agent_id, db, "monthly", 3, "reset committed"
    )

    assert granted is True
    assert (db.tries, db.rollbacks, db.queries) == (4, 3, 1)
    assert redis.store[_failure_pool_key(user_id, agent_id, "2026-01-01")] == 1


async def test_a_lost_free_counter_keeps_counting_from_its_durable_row(monkeypatch):
    """Redis lost a free-credit counter the flush had persisted at 5. The next
    free credit counts the sixth, not the first: restarted at 1, the card's
    total (the larger of row and key) stayed at 5 while the cause tally went on
    to 6, and the breakdown overshot it for the rest of the window. Only the
    user's reset lock is shared - a recharge never clears free credits."""
    redis = _cold_claim_env(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db = _BaselineDB(5)

    assert await quota_module._apply_free_refund(
        redis, user_id, agent_id, db, "monthly", "outage", "provider_unreachable"
    )

    free_key = _free_refund_key(user_id, agent_id, "2026-01-01")
    assert redis.store[free_key] == 6
    assert db.queries == 1
    assert [set(lock) for lock in db.locks] == [{"ns", "user"}]
    tally = quota_module._refund_cause_key(
        quota_module._FREE_REFUND_USAGE_TYPE, "2026-01-01", user_id
    )
    assert redis.hashes[tally] == {"provider_unreachable": 1}


async def test_a_free_credit_behind_a_stuck_reset_is_credited_but_not_counted(
    monkeypatch,
):
    """A reset of the user outlasts the wait: the row it leaves is unknown.
    The action still comes back - free credits are never refused - but the
    counter is not seeded from a guess and no cause is tallied, so a reset
    that rolls back cannot leave a key at 1 below a row at 5 that the card
    then never counts past."""
    redis = _cold_claim_env(monkeypatch)
    monkeypatch.setattr(quota_module, "_POOL_CLEAR_WAIT_SECONDS", 0.02)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db = _BaselineDB(5, clearing=10**9)

    assert await quota_module._apply_free_refund(
        redis, user_id, agent_id, db, "monthly", "outage", "provider_unreachable"
    )

    assert _free_refund_key(user_id, agent_id, "2026-01-01") not in redis.store
    assert redis.hashes == {}
    assert db.queries == 0


async def test_a_cold_claim_does_not_guess_past_a_stuck_pool_clear(monkeypatch):
    """A clear still in flight after the bounded wait leaves the pool unknown:
    the claim is not granted and seeds nothing, so whatever the clear then
    does, no refund is granted past the pool it leaves."""
    redis = _cold_claim_env(monkeypatch)
    monkeypatch.setattr(quota_module, "_POOL_CLEAR_WAIT_SECONDS", 0.02)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db = _BaselineDB(3, clearing=10**9)

    report: dict = {}
    granted = await quota_module._claim_failure_pool_slot_and_refund(
        redis, user_id, agent_id, db, "monthly", 3, "stuck clear",
        outcome_out=report,
    )

    assert granted is False
    assert report["outcome"] == quota_module.REFUND_OUTCOME_FAILED
    assert db.queries == 0
    assert _failure_pool_key(user_id, agent_id, "2026-01-01") not in redis.store


# ---------------------------------------------------------------------------
# Support codes — offline verification against the licence trust anchor
# ---------------------------------------------------------------------------


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return key, public_pem


def _mint(key, **overrides) -> str:
    """Mint a code the way ``scripts/mint_support_code.py`` does."""
    payload = {
        "v": 1,
        "kind": POOL_RECHARGE_KIND,
        "id": str(uuid.uuid4()),
        "platform": "global",
        "exp": "2099-01-01T00:00:00Z",
    }
    payload.update(overrides)
    payload = {k: v for k, v in payload.items() if v is not None}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = key.sign(raw, padding.PKCS1v15(), hashes.SHA256())
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
    return f"{SUPPORT_CODE_PREFIX}{b64(raw)}.{b64(signature)}"


def test_a_filigran_signed_code_verifies():
    key, public_pem = _keypair()
    code = _mint(key, note="SUPPORT-4821")
    verified = verify_support_code(code, public_pem, "platform-a")
    assert verified.kind == POOL_RECHARGE_KIND
    assert verified.note == "SUPPORT-4821"
    assert verified.platform_scope == "global"


def test_whitespace_and_newlines_in_a_pasted_code_are_tolerated():
    """Admins paste out of an email; a wrapped code must still verify."""
    key, public_pem = _keypair()
    code = _mint(key)
    wrapped = code[:40] + "\n  " + code[40:80] + " \t" + code[80:]
    assert verify_support_code(wrapped, public_pem, None).kind == POOL_RECHARGE_KIND


def test_a_code_signed_by_anyone_else_is_refused():
    """The whole point of signing: only the holder of the Filigran private key
    can mint a recharge."""
    attacker_key, _ = _keypair()
    _, filigran_pem = _keypair()
    with pytest.raises(SupportCodeError, match="not issued by Filigran"):
        verify_support_code(_mint(attacker_key), filigran_pem, None)


def test_a_tampered_payload_is_refused():
    """Editing the payload (e.g. to reuse an id or move the expiry) breaks the
    signature — the payload is never trusted before the signature is checked."""
    key, public_pem = _keypair()
    code = _mint(key)
    prefix, rest = code.split("-", 1)
    payload_seg, sig_seg = rest.split(".")
    forged = f"{prefix}-{payload_seg[:-4]}AAAA.{sig_seg}"
    with pytest.raises(SupportCodeError):
        verify_support_code(forged, public_pem, None)


def test_an_expired_code_is_refused():
    """A code cannot be revoked offline, so it must expire on its own."""
    key, public_pem = _keypair()
    code = _mint(key, exp="2020-01-01T00:00:00Z")
    with pytest.raises(SupportCodeError, match="expired on 2020-01-01"):
        verify_support_code(code, public_pem, None)


def test_a_code_for_another_platform_is_refused():
    """Binding the code to a PLATFORM_ID is what stops one customer's code
    from recharging every other customer's pools."""
    key, public_pem = _keypair()
    code = _mint(key, platform="platform-b")
    with pytest.raises(SupportCodeError, match="different platform"):
        verify_support_code(code, public_pem, "platform-a")
    # ...and it still verifies on the platform it was issued for.
    assert verify_support_code(code, public_pem, "platform-b").platform_scope == "platform-b"


def test_a_global_code_verifies_on_any_platform():
    key, public_pem = _keypair()
    assert verify_support_code(_mint(key), public_pem, "any-platform-id")


def test_a_valid_code_for_another_grant_is_refused():
    """A signed code is not a blank cheque: the kind it grants is checked."""
    key, public_pem = _keypair()
    code = _mint(key, kind="some_other_grant")
    with pytest.raises(SupportCodeError, match="does not grant"):
        verify_support_code(code, public_pem, None)


def test_a_code_without_an_id_is_refused():
    """No id means no way to burn it — an infinitely replayable code."""
    key, public_pem = _keypair()
    code = _mint(key, id="")
    with pytest.raises(SupportCodeError, match="cannot be tracked"):
        verify_support_code(code, public_pem, None)


def test_an_oversized_id_or_note_cannot_reach_the_ledger():
    """Signed is not sane: the fields that become a stored row are bounded."""
    key, public_pem = _keypair()
    with pytest.raises(SupportCodeError, match="no usable identifier"):
        verify_support_code(_mint(key, id="x" * 200), public_pem, None)
    verified = verify_support_code(_mint(key, note="n" * 5000), public_pem, None)
    assert len(verified.note) == 500


def test_garbage_input_is_refused_with_a_readable_message():
    _, public_pem = _keypair()
    for bad in ("", "   ", "hello", "XTMP-", "XTMP-notbase64.@@@", "XTMP-a.b.c"):
        with pytest.raises(SupportCodeError):
            verify_support_code(bad, public_pem, None)


def test_the_minting_script_and_the_verifier_agree():
    """The shipped issuing script must produce something this platform accepts
    — the two live in different repos in production, so pin the contract."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mint_support_code", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "mint_support_code.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    key, public_pem = _keypair()
    payload = {
        "v": 1,
        "kind": module.KIND,
        "id": str(uuid.uuid4()),
        "platform": "global",
        "exp": "2099-01-01T00:00:00Z",
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = key.sign(raw, padding.PKCS1v15(), hashes.SHA256())
    code = f"{module.PREFIX}{module._b64url(raw)}.{module._b64url(signature)}"
    assert verify_support_code(code, public_pem, None).kind == POOL_RECHARGE_KIND


# ---------------------------------------------------------------------------
# Recharge — source-level pins on the parts a fake session cannot exercise
# ---------------------------------------------------------------------------


def test_redemption_burns_the_code_before_it_recharges():
    """The UNIQUE flush must happen BEFORE the counters are cleared, or two
    admins pasting the same code both recharge and only one insert fails."""
    burn = inspect.getsource(pool_mod.burn_support_code)
    assert "await db.flush()" in burn
    assert "IntegrityError" in burn
    for apply_fn, clear in (
        (pool_mod.apply_pool_recharge, "_clear_pool_counters"),
        (support_codes_mod.apply_platform_quota_reset, "reset_platform_quota_usage"),
    ):
        src = inspect.getsource(apply_fn)
        assert src.index("await burn_support_code(") < src.index(clear)


def test_recharge_holds_the_flush_lock_and_clears_both_stores():
    """``flush_quota_usage`` re-persists a Redis snapshot with a monotonic
    max(), so a recharge that did not exclude it (or that skipped Redis) would
    silently leave the pools spent."""
    src = inspect.getsource(pool_mod._clear_pool_counters)
    assert "await _wait_for_flush_lock(db, redis)" in src
    assert "raise QuotaClearBusyError" in src
    assert "sa_delete(QuotaUsage)" in src
    assert "scan_iter" in src


def test_recharge_only_deletes_pool_rows():
    """A recharge must never touch action-quota usage rows."""
    src = inspect.getsource(pool_mod._clear_pool_counters)
    assert "QuotaUsage.usage_type == _POOL_USAGE_TYPE" in src


# ---------------------------------------------------------------------------
# Free credit-back — the LLM was never reached
# ---------------------------------------------------------------------------
# A run that never called a model produced nothing, so its action goes back
# whatever the pool says: the platform failed to deliver, and rationing that
# would let one provider outage eat an agent's whole allowance.


async def test_provider_outage_is_credited_back_for_free(monkeypatch):
    """The action comes back and NO pool slot is spent."""
    redis = _fake_redis(monkeypatch)
    _enable(monkeypatch, pool=3)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()

    await increment_agentic_usage(user_id, agent_id=agent_id)
    assert (
        await refund_agentic_usage_for_provider_outage(user_id, agent_id) is True
    )

    period_key = _current_period_key("monthly")
    window_key = await _window_key()
    assert redis.store[_usage_key(user_id, "agentic", None, period_key)] == 0
    assert redis.store[_global_usage_key("agentic", period_key)] == 0
    # The pool is untouched; the free tally records the incident instead.
    assert _failure_pool_key(user_id, agent_id, window_key) not in redis.store
    assert redis.store[_free_refund_key(user_id, agent_id, window_key)] == 1


async def test_provider_outages_are_not_bounded_by_the_pool(monkeypatch):
    """Ten outages in a row are all credited back — a pool of 3 does not cap
    them, and the pool stays fully available for real agent failures."""
    redis = _fake_redis(monkeypatch)
    _enable(monkeypatch, pool=3)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()

    for _ in range(10):
        await increment_agentic_usage(user_id, agent_id=agent_id)
        assert (
            await refund_agentic_usage_for_provider_outage(user_id, agent_id) is True
        )

    period_key = _current_period_key("monthly")
    window_key = await _window_key()
    assert redis.store[_usage_key(user_id, "agentic", None, period_key)] == 0
    assert redis.store[_free_refund_key(user_id, agent_id, window_key)] == 10
    # ...and the agent can still fail three times on its own merits.
    for _ in range(3):
        await increment_agentic_usage(user_id, agent_id=agent_id)
        assert await refund_agentic_usage_for_failure(user_id, agent_id) is True
    assert await refund_agentic_usage_for_failure(user_id, agent_id) is False


async def test_free_credit_back_works_with_the_feature_disabled(monkeypatch):
    """The free path is not gated on AGENT_FAILURE_QUOTA_REFUND_ENABLED: that
    flag sizes the *pool*, and an unreachable provider is not a pool question."""
    redis = _fake_redis(monkeypatch)
    monkeypatch.setattr(
        quota_module.settings, "agent_failure_quota_refund_enabled", False
    )
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()

    await increment_agentic_usage(user_id, agent_id=agent_id)
    assert (
        await refund_agentic_usage_for_provider_outage(user_id, agent_id) is True
    )

    period_key = _current_period_key("monthly")
    assert redis.store[_usage_key(user_id, "agentic", None, period_key)] == 0


async def test_free_credit_back_never_raises(monkeypatch):
    """Same contract as the pool path: the caller is already handling a failure."""
    monkeypatch.setattr(
        quota_module,
        "redis_client",
        AsyncMock(side_effect=RuntimeError("redis unreachable")),
    )
    assert (
        await refund_agentic_usage_for_provider_outage(uuid.uuid4(), uuid.uuid4())
        is False
    )


async def test_free_tally_is_a_separate_usage_type(monkeypatch):
    """The tally must not be mistaken for the pool, nor for billable usage."""
    _enable(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    free = _free_refund_key(user_id, agent_id, "2026-01-01")
    pool = _failure_pool_key(user_id, agent_id, "2026-01-01")

    assert free != pool
    assert quota_module._FREE_REFUND_USAGE_TYPE not in quota_module._QUOTA_TYPES
    assert quota_module._FREE_REFUND_USAGE_TYPE != quota_module._POOL_USAGE_TYPE
    # Same durable key space, so the flush persists it across a year-long window.
    assert free.startswith(quota_module._USAGE_PREFIX)


# ---------------------------------------------------------------------------
# Which failures are free — the classifier the call sites route on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "APIConnectionError: Connection error.",
        "anthropic.InternalServerError: Overloaded",
        "Error code: 503 - {'error': 'service unavailable'}",
        "Error code: 529 - overloaded_error",
        "upstream connect error or disconnect/reset before headers",
        "No LLM provider configured for this platform",
        "Request timed out.",
    ],
)
def test_unreachable_provider_is_free(message):
    assert is_provider_unavailable_error(Exception(message)) is True


@pytest.mark.parametrize(
    "message",
    [
        # The provider answered — it just refused THIS traffic. Rate limits
        # track how much the caller is asking for, which is exactly what the
        # pool's bound is for.
        "Error code: 429 - rate_limit_error",
        "Too many requests",
        # The payload is the problem, and it reproduces on every provider.
        "request_too_large: request exceeds the maximum size",
        # Ordinary agent failures.
        "KeyError: 'tool_result'",
        "The model returned invalid JSON",
    ],
)
def test_everything_else_comes_out_of_the_pool(message):
    assert is_provider_unavailable_error(Exception(message)) is False


def test_exhausted_failover_chain_is_classified_on_its_last_failure():
    """``AllProvidersFailedError``'s own message names providers, not causes —
    classifying on the wrapper would make every failover an outage."""
    from app.llm.router import AllProvidersFailedError

    outage = AllProvidersFailedError(
        [("anthropic", Exception("Overloaded")), ("openai", Exception("Connection error."))]
    )
    size = AllProvidersFailedError(
        [("anthropic", Exception("Overloaded")), ("openai", Exception("request_too_large"))]
    )
    throttled = AllProvidersFailedError([("anthropic", Exception("rate limit"))])

    assert is_provider_unavailable_error(outage) is True
    assert is_provider_unavailable_error(size) is False
    assert is_provider_unavailable_error(throttled) is False


def test_bare_transport_exceptions_are_free():
    """A bare ``TimeoutError`` carries no message at all, so keyword matching
    alone would miss it."""
    assert is_provider_unavailable_error(TimeoutError()) is True
    assert is_provider_unavailable_error(ConnectionError()) is True


# ---------------------------------------------------------------------------
# Routing — source-level pins
# ---------------------------------------------------------------------------


def test_chat_routes_provider_outages_to_the_free_credit():
    """One helper decides, for every chat failure path, which budget pays."""
    assert "if free or (exc is not None and is_provider_unavailable_error(exc)):" in _CHAT_SRC
    assert "refund_agentic_usage_for_provider_outage(" in _CHAT_SRC
    # The generic and setup handlers must hand the exception over, or every
    # failure would silently come out of the pool.
    assert 'await _refund_failed_turn("agent loop failure", exc)' in _CHAT_SRC
    assert 'await _refund_failed_turn("setup failure", exc, free=True)' in _CHAT_SRC


def test_llm_resolution_failure_is_a_counted_free_credit():
    """It used to call the bare refund, which reversed the action but recorded
    nothing — the admin card would show an unexplained gap."""
    assert "LLM resolution failure" in _CHAT_SRC
    resolution_at = _CHAT_SRC.index("LLM resolution failure")
    free_at = _CHAT_SRC.rindex(
        "refund_agentic_usage_for_provider_outage", 0, resolution_at
    )
    assert free_at < resolution_at


def test_context_overflow_and_413_stay_pool_funded():
    """The provider answered in both cases: the request was too big for it."""
    lines = _CHAT_SRC.splitlines()
    for marker in ("context overflow", "request too large"):
        line = next(ln for ln in lines if f'_refund_failed_turn("{marker}"' in ln)
        assert "exc" not in line.split(marker)[1]


def test_assignment_and_extension_route_on_the_same_classifier():
    for src in (
        inspect.getsource(assignment_engine_mod.execute_assignment_run),
        inspect.getsource(extension_mod),
    ):
        assert "is_provider_unavailable_error(" in src
        assert "refund_agentic_usage_for_provider_outage(" in src
        assert "refund_agentic_usage_for_failure(" in src


# ---------------------------------------------------------------------------
# Abandoned runs — the failure path that never reaches the failure handler
# ---------------------------------------------------------------------------
# The orchestration engine can give up on a run from another process entirely
# (lease expired, worker killed, deadline blown mid-run). ``on_dead_letter``
# then marks it failed, and ``execute_assignment_run``'s own handler never runs
# again — so the credit-back has to live there too, or an action charged
# up-front stays charged forever for a run the platform simply dropped.


def test_metering_is_recorded_durably_on_the_run():
    """The out-of-process dead-letter path has no in-memory state, so the
    charge has to be readable from the run row itself - with the attempt that
    made it."""
    from app.services import attempt_charges

    src = inspect.getsource(assignment_engine_mod.execute_assignment_run)
    assert "await claim_charge_record(" in src
    context = attempt_charges.with_recorded_charge(
        {"type": "schedule"}, charge_id="c1", user_id=None
    )
    assert context == {
        "type": "schedule",
        "quota_metered": True,
        "quota_metered_user_id": None,
        "quota_metered_charge": "c1",
    }
    # Reassigned, not mutated: SQLAlchemy does not track in-place JSON writes.
    assert "run.trigger_context = with_recorded_charge(" in inspect.getsource(
        attempt_charges.claim_charge_record
    )


def test_abandoned_run_is_credited_back_for_free():
    from app.services.orchestration_adapters import assignment as adapter_mod

    src = inspect.getsource(adapter_mod.AssignmentAdapter.on_dead_letter)
    assert "refund_agentic_usage_for_provider_outage(" in src
    # Never from the pool: every route to dead_letter is infrastructure, so
    # rationing it would let one worker crash spend a whole year's allowance.
    assert "refund_agentic_usage_for_failure(" not in src


def test_abandoned_run_credit_is_gated_on_the_durable_charge():
    """A run that never started, or one a fail-open quota backend let through
    unmetered, must not be credited — that would take an action from a
    different run."""
    from app.services import attempt_charges
    from app.services.orchestration_adapters import assignment as adapter_mod

    src = inspect.getsource(adapter_mod.AssignmentAdapter.on_dead_letter)
    gate_at = src.index("charge = recorded_charge(run.trigger_context)")
    refund_at = src.index("refund_agentic_usage_for_provider_outage(")
    assert gate_at < refund_at
    assert "context.get(QUOTA_METERED_KEY)" in inspect.getsource(
        attempt_charges.recorded_charge
    )


def test_dead_letter_never_credits_an_already_terminal_run():
    """``execute_assignment_run``'s handler may have credited it already; the
    terminal-status guard is what stops the second credit."""
    from app.services.orchestration_adapters import assignment as adapter_mod

    src = inspect.getsource(adapter_mod.AssignmentAdapter.on_dead_letter)
    guard_at = src.index("TERMINAL_RUN_STATUSES")
    refund_at = src.index("refund_agentic_usage_for_provider_outage(")
    assert guard_at < refund_at


# ---------------------------------------------------------------------------
# Quota-exempt (platform) agents
# ---------------------------------------------------------------------------


def test_exempt_runs_can_never_produce_a_pool_row():
    """A seeded platform automation is never metered, so it never reaches a
    refund — the card cannot show pool consumption for it. This pins the gate
    the whole claim rests on."""
    src = inspect.getsource(assignment_engine_mod.execute_assignment_run)
    exempt_at = src.index("_run_is_quota_exempt(agent, run)")
    increment_at = src.index("await increment_agentic_usage(")
    assert exempt_at < increment_at


def test_platform_agents_are_flagged_not_filtered():
    """A builtin agent's MANUAL runs are charged like any other, so a row for
    one is real. Hiding it would make the card disagree with the quota gauges;
    the UI labels it instead."""
    src = inspect.getsource(pool_mod.get_pool_status)
    assert '"agent_is_builtin"' in src
    assert "Agent.is_builtin" in src


def test_a_turn_that_died_in_setup_is_free_without_classifying_anything():
    """The setup window never reached a model, so the exception's text is
    irrelevant — a decrypt failure, a broken binding and a missing provider all
    mean the same thing there."""
    assert 'await _refund_failed_turn("setup failure", exc, free=True)' in _CHAT_SRC


def test_assignment_before_llm_resolution_is_free():
    """Same rule on the assignment path: a run that never had a model to call
    (no provider, undecryptable credential, setup blew up) is credited free."""
    src = inspect.getsource(assignment_engine_mod.execute_assignment_run)
    assert "_llm_resolved = False" in src
    assert "_llm_resolved = True" in src
    # The decision is bound to a name rather than tested inline:
    # the outcome is now ALSO recorded on the trace ("credited free" vs "from
    # the pool"), so it is needed twice. The predicate itself is unchanged, and
    # that is what this pins — free is decided on "we never reached a model",
    # never on a keyword in the error text.
    assert "not _llm_resolved or is_provider_unavailable_error(e)" in src
    # The flag must be initialised with the other pre-try locals (the cluster
    # commented "Initialise before try so except handler always has it"), or
    # the handler trips over an unbound local for exactly the early failures it
    # is meant to catch — then set only once resolution has returned.
    assert (
        src.index("wf_trace_ctx = None")
        < src.index("_llm_resolved = False")
        < src.index("await get_llm_for_user(")
        < src.index("_llm_resolved = True")
        < src.index("not _llm_resolved or is_provider_unavailable_error(e)")
    )


# ---------------------------------------------------------------------------
# Metering receipts — for work abandoned by a process that never sees the turn
# ---------------------------------------------------------------------------
# A sub-agent task is charged INSIDE its agent turn. When the orchestration
# engine gives up on it (worker killed, lease expired), that turn never runs
# again, so the credit-back has to happen elsewhere — and it must not GUESS
# that a charge happened: crediting one that never occurred takes an action
# from somebody else's run in the same period.


async def test_a_charge_writes_a_receipt_only_when_asked(monkeypatch):
    redis = _fake_redis(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()

    await increment_agentic_usage(user_id, agent_id=agent_id)
    assert not [k for k in redis.store if k.startswith("quota:receipt:")]

    await increment_agentic_usage(
        user_id, agent_id=agent_id, receipt_token="sub_agent_task:abc"
    )
    assert redis.store["quota:receipt:sub_agent_task:abc"]


async def test_receipt_credits_the_action_back_for_free(monkeypatch):
    redis = _fake_redis(monkeypatch)
    _enable(monkeypatch, pool=3)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()

    await increment_agentic_usage(
        user_id, agent_id=agent_id, receipt_token="sub_agent_task:t1"
    )
    period_key = _current_period_key("monthly")
    assert redis.store[_usage_key(user_id, "agentic", None, period_key)] == 1

    assert await refund_agentic_usage_by_receipt("sub_agent_task:t1") is True

    assert redis.store[_usage_key(user_id, "agentic", None, period_key)] == 0
    window_key = await _window_key()
    # Free: the tally moved, the pool did not.
    assert redis.store[_free_refund_key(user_id, agent_id, window_key)] == 1
    assert _failure_pool_key(user_id, agent_id, window_key) not in redis.store


async def test_a_receipt_can_only_be_redeemed_once(monkeypatch):
    """Two dead-letter passes over the same job — a retried reconciler cycle,
    two replicas — must not credit twice. Redeeming marks the receipt
    redeemed, atomically, and that is what guarantees it."""
    redis = _fake_redis(monkeypatch)
    _enable(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()

    await increment_agentic_usage(user_id, agent_id=agent_id)  # another run's usage
    await increment_agentic_usage(
        user_id, agent_id=agent_id, receipt_token="sub_agent_task:t2"
    )
    period_key = _current_period_key("monthly")
    assert redis.store[_usage_key(user_id, "agentic", None, period_key)] == 2

    assert await refund_agentic_usage_by_receipt("sub_agent_task:t2") is True
    assert await refund_agentic_usage_by_receipt("sub_agent_task:t2") is False
    assert await refund_agentic_usage_by_receipt("sub_agent_task:t2") is False

    # Exactly one action back — the other run's charge survives.
    assert redis.store[_usage_key(user_id, "agentic", None, period_key)] == 1


async def test_work_that_was_never_charged_gets_nothing(monkeypatch):
    """No receipt = never charged (quota refused it, or it never got that far).
    Crediting anyway would take an action from another run."""
    redis = _fake_redis(monkeypatch)
    other_user = uuid.uuid4()
    await increment_agentic_usage(other_user, agent_id=uuid.uuid4())
    period_key = _current_period_key("monthly")

    assert await refund_agentic_usage_by_receipt("sub_agent_task:never") is False
    assert redis.store[_usage_key(other_user, "agentic", None, period_key)] == 1


async def test_receipt_refund_never_raises(monkeypatch):
    monkeypatch.setattr(
        quota_module,
        "redis_client",
        AsyncMock(side_effect=RuntimeError("redis unreachable")),
    )
    assert await refund_agentic_usage_by_receipt("sub_agent_task:x") is False


async def test_a_corrupt_receipt_is_ignored(monkeypatch):
    """A receipt is data read back from a cache — it must not be trusted to
    parse."""
    redis = _fake_redis(monkeypatch)
    redis.store["quota:receipt:sub_agent_task:bad"] = "not-json"
    assert await refund_agentic_usage_by_receipt("sub_agent_task:bad") is False


def test_the_receipt_is_written_after_the_counters():
    """A receipt must never outlive the charge it attests to: if the counters
    fail, no receipt exists and nothing is credited later."""
    src = inspect.getsource(quota_module.increment_agentic_usage)
    receipt = src.index("_receipt_key(receipt_token)")
    assert src.index('_count_global(redis, "agentic"') < receipt
    assert src.index('_usage_key(user_id, "agentic", None, period_key, agent_id)') < receipt


def test_sub_agent_tasks_carry_a_receipt_token():
    import app.services.sub_agent as sub_agent_mod
    from app.services.attempt_charges import task_receipt_token

    src = inspect.getsource(sub_agent_mod._execute_sub_agent_task)
    assert "quota_receipt_token=task_receipt_token(task_id, attempt=attempt)" in src
    # One receipt per orchestration attempt; the task-wide one of an
    # older release outside the engine.
    assert task_receipt_token("t-1", attempt=2) == "sub_agent_task:t-1:2"
    assert task_receipt_token("t-1", attempt=None) == "sub_agent_task:t-1"


def test_abandoned_sub_agent_task_is_credited_by_receipt():
    from app.services import attempt_charges
    from app.services.orchestration_adapters import sub_agent as adapter_mod

    src = inspect.getsource(adapter_mod.SubAgentAdapter.on_dead_letter)
    assert "credit_abandoned_task(" in src
    credit = inspect.getsource(attempt_charges.credit_abandoned_task)
    assert "refund_agentic_usage_by_receipt(" in credit
    # Free only — an abandoned job is infrastructure, not an agent failure.
    assert "refund_agentic_usage_for_failure(" not in src
    assert "refund_agentic_usage_for_failure(" not in credit


def test_ordinary_chat_turns_write_no_receipt():
    """A chat turn dies with its own handlers, so it needs no receipt — and one
    per turn would be a pointless write on the hottest path there is."""
    assert "quota_receipt_token: str | None = None," in _CHAT_SRC
    # run_agent_chat forwards whatever its caller named (None for a chat turn)...
    assert "receipt_token=quota_receipt_token," in _CHAT_SRC
    # ...and the quota gate hands it to the charge.
    assert "receipt_token=receipt_token" in inspect.getsource(
        agent_chat_mod.enforce_agentic_quota_ex
    )


# ---------------------------------------------------------------------------
# What the provider REPLIED is never free
# ---------------------------------------------------------------------------
# The free path is unbounded and not gated by the enable flag, so it must stay
# strictly "no model was reached". Anything the provider answers — a throttle,
# a rejected credential, an account out of credit — is persistent and
# indefinitely reproducible: forgiving it there would stop the action quota
# metering for as long as it lasts, which is the free-unlimited-retries
# outcome the pool's bound exists to prevent.


@pytest.mark.parametrize(
    "message",
    [
        "Error code: 429 - rate_limit_error",
        "Too many requests",
        "Error code: 401 - invalid_api_key",
        "authentication_error: invalid x-api-key",
        "permission_denied",
        "insufficient_quota: your account has run out of credits",
        "Your credit balance is too low to access the API",
        # Broad substrings that used to reach the free path: a docs URL in an
        # error body must not decide who pays.
        "See https://console.anthropic.com/account/billing for details",
    ],
)
def test_a_provider_that_answered_is_never_free(message):
    assert is_provider_unavailable_error(Exception(message)) is False


@pytest.mark.parametrize(
    "message",
    [
        # A bare "429" matched "...4290 tokens" — right answer, wrong reason.
        "Prompt used 4290 tokens",
        # "internal server error" is also what a PLATFORM bug stringifies to.
        "Internal Server Error in app/services/foo.py",
        # A bare "timeout"/"unreachable" substring must not hand a tool crash
        # the unbounded path.
        "ToolError: the remote endpoint is unreachable per its own docs",
    ],
)
def test_broad_substrings_no_longer_reach_the_free_path(message):
    assert is_provider_unavailable_error(Exception(message)) is False


@pytest.mark.parametrize(
    "message",
    [
        "Request timed out.",
        "httpx.ReadTimeout: read timeout",
        "connect timeout after 10s",
        "Deadline exceeded",
    ],
)
def test_provider_shaped_timeouts_are_still_free(message):
    """Dropping the bare token must not lose the real case."""
    assert is_provider_unavailable_error(Exception(message)) is True


async def test_a_dry_provider_account_stops_being_forgiven(monkeypatch):
    """THE property at risk, and the reason item 1 of the review was a blocker.

    An exhausted provider account fails every run the same way, forever. If it
    took the free path, the quota would stop metering entirely until someone
    topped the account up. It must come out of the bounded pool and run out.
    """
    redis = _fake_redis(monkeypatch)
    _enable(monkeypatch, pool=5)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    dry_account = Exception("insufficient_quota: your account has run out of credits")

    assert is_provider_unavailable_error(dry_account) is False

    granted = []
    for _ in range(8):
        await increment_agentic_usage(user_id, agent_id=agent_id)
        # What the call sites do: free only when no model was reached.
        if is_provider_unavailable_error(dry_account):
            granted.append(
                await refund_agentic_usage_for_provider_outage(user_id, agent_id)
            )
        else:
            granted.append(await refund_agentic_usage_for_failure(user_id, agent_id))

    # Five forgiven, then the quota starts metering again — the whole point.
    assert granted == [True] * 5 + [False] * 3
    period_key = _current_period_key("monthly")
    assert redis.store[_usage_key(user_id, "agentic", None, period_key)] == 3
    # And nothing leaked onto the unbounded tally.
    window_key = await _window_key()
    assert _free_refund_key(user_id, agent_id, window_key) not in redis.store


def test_a_cancelled_turn_is_still_never_refunded():
    """Pinned so a future refactor cannot quietly start refunding cancellations:
    awaiting inside a cancelled task re-raises, and a cancellation is not a
    failure of the agent."""
    assert "if isinstance(exc, Exception):" in _CHAT_SRC
    guard_at = _CHAT_SRC.index("if isinstance(exc, Exception):")
    refund_at = _CHAT_SRC.index('_refund_failed_turn("setup failure"', guard_at)
    assert guard_at < refund_at


def test_a_platform_scoped_code_is_refused_when_we_cannot_identify_ourselves():
    """Fail closed: accepting another install's code because PLATFORM_ID is
    unset would make the binding decorative."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    payload = {
        "v": 1,
        "kind": POOL_RECHARGE_KIND,
        "id": str(uuid.uuid4()),
        "platform": "someone-elses-platform",
        "exp": "2099-01-01T00:00:00Z",
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = key.sign(raw, padding.PKCS1v15(), hashes.SHA256())
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
    code = f"{SUPPORT_CODE_PREFIX}{b64(raw)}.{b64(sig)}"

    with pytest.raises(SupportCodeError, match="PLATFORM_ID"):
        verify_support_code(code, public_pem, None)
    with pytest.raises(SupportCodeError, match="PLATFORM_ID"):
        verify_support_code(code, public_pem, "")
    # ...and it still verifies on the platform it names.
    assert verify_support_code(code, public_pem, "someone-elses-platform")


def test_the_status_read_batches_instead_of_a_get_per_key():
    """It runs on a synchronous admin endpoint; a GET per (user, agent) pair
    made the card cost a round trip each."""
    src = inspect.getsource(pool_mod.get_pool_status)
    assert "redis.mget(keys)" in src
    assert "await redis.get(" not in src


def test_the_recharge_sweep_is_batched_and_bounded():
    src = inspect.getsource(pool_mod._clear_pool_counters)
    assert "_stash_counters(redis, stash, batch, seeds)" in src
    assert "_SWEEP_MAX_KEYS" in src
    # A partial sweep must be loud: it leaves pools spent.
    assert "logger.error(" in src
    # The Redis sweep stays inside the advisory lock — moving it out would
    # reopen the race the lock exists for.
    assert src.index("_wait_for_flush_lock") < src.index("scan_iter")
    assert src.index("scan_iter") < src.index("await db.commit()")


def test_the_recharge_reports_both_counts_not_a_max():
    src = inspect.getsource(pool_mod._clear_pool_counters)
    assert '"rows_deleted"' in src and '"keys_cleared"' in src
    assert "max(deleted, cleared)" not in src


def test_free_credits_alert_without_refusing(monkeypatch):
    """Unbounded by design, but not unwatched."""
    src = inspect.getsource(quota_module._tally_free_credit)
    assert "_FREE_CREDIT_ALERT_FACTOR" in src
    assert "logger.error(" in src
    # It must never turn into a refusal: the incident is still ours.
    assert "return False" not in src


# ---------------------------------------------------------------------------
# Unlimited quotas — nothing was rationed, so the pool is never touched
# ---------------------------------------------------------------------------
# The usage counters are incremented whether or not anything caps them (they
# double as consumption telemetry), so a failed run always LOOKS charged. On a
# platform with no quota that charge costs nothing, and claiming a pool slot
# for it only ever produced "exhausted its failure quota pool - failed run
# stays charged" for a user nobody was rationing.

_TEST_WINDOW = _window_from_anchor(
    datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 9, 22, tzinfo=UTC), "test"
)


def _private_session(monkeypatch) -> object:
    """Route the refund path's private session to a stub.

    Everything that would query it — the limit predicate, the pool window,
    the durable baseline and the period resolution of the refunded counters —
    is pinned separately, so the stub never has to answer a query and the test
    is about the pool's decision alone.
    """
    session = object()

    @contextlib.asynccontextmanager
    async def _factory():
        yield session

    monkeypatch.setattr("app.core.database.async_session", _factory)
    monkeypatch.setattr(
        quota_module, "_load_pool_baseline", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(
        quota_module,
        "_resolve_agentic_periods",
        AsyncMock(return_value=("monthly", set())),
    )
    monkeypatch.setattr(
        "app.services.agent_failure_pool.resolve_pool_window",
        AsyncMock(return_value=_TEST_WINDOW),
    )
    return session


async def test_an_unlimited_quota_never_touches_the_pool(monkeypatch):
    """A user nothing caps: however many times the agent fails, no slot is
    claimed, nothing is credited back, and the outcome says why."""
    redis = _fake_redis(monkeypatch)
    _enable(monkeypatch, pool=2)
    _private_session(monkeypatch)
    predicate = AsyncMock(return_value=False)
    monkeypatch.setattr(quota_module, "has_enforceable_agentic_limit", predicate)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()

    outcomes = []
    for _ in range(5):
        await increment_agentic_usage(user_id, agent_id=agent_id)
        report: dict = {}
        granted = await refund_agentic_usage_for_failure(
            user_id, agent_id, db=object(), outcome_out=report
        )
        assert granted is False
        outcomes.append(report["outcome"])

    assert outcomes == [quota_module.REFUND_OUTCOME_QUOTA_UNLIMITED] * 5
    # The pool was never claimed — the counter does not even exist, so the
    # card cannot show "Spent" and the log cannot say "exhausted".
    assert _failure_pool_key(user_id, agent_id, _TEST_WINDOW.key) not in redis.store
    # The counters keep counting: they are consumption telemetry here, and
    # nothing was reversed because nothing was rationed.
    period_key = _current_period_key("monthly")
    assert redis.store[_usage_key(user_id, "agentic", None, period_key)] == 5
    assert predicate.await_count == 5


async def test_a_limited_user_is_pooled_exactly_as_before(monkeypatch):
    """The gate changes nothing for a user who does carry a limit: the first
    failures are forgiven, the pool then holds and the charge stands."""
    redis = _fake_redis(monkeypatch)
    _enable(monkeypatch, pool=2)
    _private_session(monkeypatch)
    monkeypatch.setattr(
        quota_module, "has_enforceable_agentic_limit", AsyncMock(return_value=True)
    )
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()

    granted, outcomes = [], []
    for _ in range(3):
        await increment_agentic_usage(user_id, agent_id=agent_id)
        report: dict = {}
        granted.append(
            await refund_agentic_usage_for_failure(
                user_id, agent_id, db=object(), outcome_out=report
            )
        )
        outcomes.append(report["outcome"])

    assert granted == [True, True, False]
    assert outcomes == [
        quota_module.REFUND_OUTCOME_CREDITED,
        quota_module.REFUND_OUTCOME_CREDITED,
        quota_module.REFUND_OUTCOME_POOL_EXHAUSTED,
    ]
    assert redis.store[_failure_pool_key(user_id, agent_id, _TEST_WINDOW.key)] == 2
    period_key = _current_period_key("monthly")
    assert redis.store[_usage_key(user_id, "agentic", None, period_key)] == 1


async def test_without_a_session_the_pool_applies_as_it_always_did(monkeypatch):
    """``db=None`` is the degraded path: the question cannot be answered, so
    the pool applies rather than guessing "unlimited" and forgiving nothing."""
    _fake_redis(monkeypatch)
    _enable(monkeypatch, pool=1)
    predicate = AsyncMock(return_value=False)
    monkeypatch.setattr(quota_module, "has_enforceable_agentic_limit", predicate)
    report: dict = {}

    assert (
        await refund_agentic_usage_for_failure(
            uuid.uuid4(), uuid.uuid4(), outcome_out=report
        )
        is True
    )
    assert report["outcome"] == quota_module.REFUND_OUTCOME_CREDITED
    predicate.assert_not_awaited()


async def test_a_disabled_pool_and_a_broken_refund_report_themselves(monkeypatch):
    """The other two ``False`` readings are named too, so a trace never has to
    guess between "spent", "off", "broken" and "nothing to give back"."""
    monkeypatch.setattr(
        quota_module.settings, "agent_failure_quota_refund_enabled", False
    )
    report: dict = {}
    assert (
        await refund_agentic_usage_for_failure(
            uuid.uuid4(), uuid.uuid4(), outcome_out=report
        )
        is False
    )
    assert report["outcome"] == quota_module.REFUND_OUTCOME_POOL_DISABLED

    _enable(monkeypatch)
    monkeypatch.setattr(
        quota_module,
        "redis_client",
        AsyncMock(side_effect=RuntimeError("redis unreachable")),
    )
    report = {}
    assert (
        await refund_agentic_usage_for_failure(
            uuid.uuid4(), uuid.uuid4(), outcome_out=report
        )
        is False
    )
    assert report["outcome"] == quota_module.REFUND_OUTCOME_FAILED


# The predicate mirrors ``check_agentic_quota`` tier for tier. Each tier is
# pinned on its own so a change to the gate's resolution has to change this
# file too.


async def test_the_license_ceiling_alone_makes_the_quota_enforced(monkeypatch):
    """XTM One: a non-zero license period caps everyone, whatever their own
    config says — the per-user config is not even consulted."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(
        quota_module, "get_xtm_agentic_ceilings", AsyncMock(return_value={"daily": 30})
    )
    resolve = AsyncMock(return_value=None)
    monkeypatch.setattr(quota_module, "resolve_quota_config", resolve)

    assert await quota_module.has_enforceable_agentic_limit(object(), object(), uuid.uuid4()) is True
    resolve.assert_not_awaited()


@pytest.mark.parametrize(
    ("config", "enforced"),
    [
        # No config at any tier: the resolver's "no quota".
        (None, False),
        # The UI's "Unlimited" — the platform default with no number.
        ({"default": None, "period": "monthly"}, False),
        # A number is a limit; zero included, exactly as it is for the gate.
        ({"default": 0, "period": "monthly"}, True),
        ({"default": 100, "period": "monthly"}, True),
        # The license ceiling stood in for the per-user config: enforced.
        ({"default": 30, "period": "daily", "global": True}, True),
    ],
)
async def test_an_infinite_license_falls_through_to_the_users_own_config(
    monkeypatch, config, enforced
):
    """XTM One with every license period at infinity (the production case):
    what remains is the user > group > platform-default resolution the gate
    itself runs, read through the same function."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(
        quota_module, "get_xtm_agentic_ceilings", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        quota_module, "resolve_quota_config", AsyncMock(return_value=config)
    )

    assert (
        await quota_module.has_enforceable_agentic_limit(object(), object(), uuid.uuid4())
        is enforced
    )


async def test_copilot_platform_limit_is_the_platform_tier(monkeypatch):
    """Copilot mode has no license: the platform tier is ``platform_limit``."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "copilot")
    monkeypatch.setattr(
        quota_module,
        "_load_platform_wide_config",
        AsyncMock(return_value={"platform_limit": 500, "period": "monthly"}),
    )
    resolve = AsyncMock(return_value=None)
    monkeypatch.setattr(quota_module, "resolve_quota_config", resolve)

    assert await quota_module.has_enforceable_agentic_limit(object(), object(), uuid.uuid4()) is True
    resolve.assert_not_awaited()

    monkeypatch.setattr(
        quota_module, "_load_platform_wide_config", AsyncMock(return_value=None)
    )
    assert await quota_module.has_enforceable_agentic_limit(object(), object(), uuid.uuid4()) is False


async def test_the_platform_tier_can_be_passed_in_once(monkeypatch):
    """The status card resolves many users; reading the license per user would
    parse the PEM each time, so the caller may pass the platform tier once."""
    ceilings = AsyncMock(return_value=None)
    monkeypatch.setattr(quota_module.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(quota_module, "get_xtm_agentic_ceilings", ceilings)
    monkeypatch.setattr(
        quota_module, "resolve_quota_config", AsyncMock(return_value=None)
    )

    assert (
        await quota_module.has_enforceable_agentic_limit(
            object(), object(), uuid.uuid4(), platform_cap_enforced=True
        )
        is True
    )
    assert (
        await quota_module.has_enforceable_agentic_limit(
            object(), object(), uuid.uuid4(), platform_cap_enforced=False
        )
        is False
    )
    ceilings.assert_not_awaited()


@pytest.mark.parametrize(
    ("cap", "global_cfg", "unlimited"),
    [
        (True, {}, False),
        (False, {}, True),
        (False, {"agentic": {"default": None, "period": "monthly"}}, True),
        (False, {"agentic": {"default": 50, "period": "monthly"}}, False),
        (False, {"agentic": {"default": 0, "period": "monthly"}}, False),
    ],
)
async def test_platform_wide_unlimited_means_no_cap_and_no_default(
    monkeypatch, cap, global_cfg, unlimited
):
    """The card's notice: nothing platform-wide caps anyone. Users and groups
    may still carry their own limit, which is the per-user predicate's job."""
    monkeypatch.setattr(
        quota_module, "_load_global_quota_config", AsyncMock(return_value=global_cfg)
    )
    assert (
        await quota_module.platform_agentic_quota_is_unlimited(
            object(), platform_cap_enforced=cap
        )
        is unlimited
    )


def test_the_status_card_never_says_spent_about_an_unlimited_user():
    """The read model runs the SAME predicate as the refund path, once per
    user with the platform tier read once, and a row of a user the pool does
    not apply to is neither "exhausted" nor counted under "pools exhausted"."""
    src = inspect.getsource(pool_mod.get_pool_status)
    assert "has_enforceable_agentic_limit(" in src
    assert "platform_cap_enforced=False" in src
    assert '"quota_unlimited": user_str in unlimited_users' in src
    assert "and user_str not in unlimited_users" in src
    assert '"platform_quota_unlimited": platform_quota_unlimited' in src


class _StatusRedis(_FakeRedis):
    """The counter store as ``get_pool_status`` reads it: a keyspace scan plus
    one ``MGET``, never a GET per key."""

    async def scan_iter(self, match="*", count=None):
        import fnmatch

        for key in list(self.store):
            if fnmatch.fnmatch(key, match):
                yield key

    async def mget(self, keys):
        return [self.store.get(key) for key in keys]


class _StatusSession:
    """Answers the four selects of ``get_pool_status`` from dicts.

    The durable rows are left empty so every counter comes from Redis - the
    merge of the two is not what these tests are about.
    """

    def __init__(self, agents: dict[str, str], users: dict[str, str]):
        self._agents = agents
        self._users = users

    async def execute(self, stmt):
        from types import SimpleNamespace

        from app.models.agent import Agent
        from app.models.quota import QuotaUsage
        from app.models.support_code import SupportCodeRedemption
        from app.models.user import User

        entity = stmt.column_descriptions[0]["entity"]
        rows: list = []
        if entity is Agent:
            rows = [
                SimpleNamespace(id=uuid.UUID(agent_id), name=name, is_builtin=False)
                for agent_id, name in self._agents.items()
            ]
        elif entity is User:
            rows = [
                SimpleNamespace(id=uuid.UUID(user_id), email=email)
                for user_id, email in self._users.items()
            ]
        else:
            assert entity in (QuotaUsage, SupportCodeRedemption), entity

        class _Result:
            def all(self):
                return rows

            def scalars(self):
                return self

        return _Result()


async def test_the_status_card_reads_the_predicate_only_where_it_can_change_a_row(
    monkeypatch,
):
    """Four users with pool rows, a card that serves ONE row. The predicate is
    resolved for the served row and for every row that would otherwise read
    "exhausted" - nobody else - so the exhausted total is exact while a long
    tail of historical rows costs nothing per admin page load. An unlimited
    user's spent pool reads "not applicable", never "Spent"."""
    redis = _StatusRedis()
    monkeypatch.setattr(pool_mod, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(
        pool_mod, "resolve_pool_window", AsyncMock(return_value=_TEST_WINDOW)
    )
    monkeypatch.setattr(pool_mod, "read_refund_causes", AsyncMock(return_value={}))
    monkeypatch.setattr(
        pool_mod, "agentic_platform_cap_enforced", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        pool_mod, "platform_agentic_quota_is_unlimited", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(pool_mod, "_STATUS_MAX_ROWS", 1)
    _enable(monkeypatch, pool=3)

    # (agent name, slots used). Sorted worst first by ``used`` then by agent
    # name, so ``a`` is the one served row and ``d`` the exhausted row that
    # falls outside the served slice.
    fixtures = {
        "a": ("agent-a", 3),  # exhausted, unlimited quota
        "d": ("agent-d", 3),  # exhausted, limited quota
        "b": ("agent-b", 2),
        "c": ("agent-c", 1),
    }
    user_ids = {tag: uuid.uuid4() for tag in fixtures}
    agent_ids = {tag: uuid.uuid4() for tag in fixtures}
    for tag, (_name, used) in fixtures.items():
        key = _usage_key(
            user_ids[tag],
            quota_module._POOL_USAGE_TYPE,
            None,
            _TEST_WINDOW.key,
            agent_ids[tag],
        )
        redis.store[key] = used
    unlimited = {user_ids["a"]}

    async def _predicate(_db, _redis, user_id, *, platform_cap_enforced=None):
        assert platform_cap_enforced is False, "the platform tier is read once"
        return user_id not in unlimited

    predicate = AsyncMock(side_effect=_predicate)
    monkeypatch.setattr(pool_mod, "has_enforceable_agentic_limit", predicate)

    status = await pool_mod.get_pool_status(
        _StatusSession(
            {str(agent_ids[t]): name for t, (name, _u) in fixtures.items()},
            {str(uid): f"{tag}@example.com" for tag, uid in user_ids.items()},
        )
    )

    resolved = {call.args[2] for call in predicate.await_args_list}
    assert resolved == {user_ids["a"], user_ids["d"]}

    assert status["platform_quota_unlimited"] is True
    assert status["truncated"] is True
    (served,) = status["entries"]
    assert served["agent_name"] == "agent-a"
    assert served["quota_unlimited"] is True
    assert served["exhausted"] is False
    assert served["used"] == 3
    # ``d`` is not served, but it IS the one exhausted pool on the platform.
    assert status["totals"]["exhausted"] == 1
    assert status["totals"]["agents_with_failures"] == 4


async def test_the_status_card_skips_the_predicate_under_a_platform_cap(monkeypatch):
    """A license ceiling or a ``platform_limit`` caps everyone: nobody is
    unlimited, so nobody is resolved and every spent pool reads exhausted."""
    redis = _StatusRedis()
    monkeypatch.setattr(pool_mod, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(
        pool_mod, "resolve_pool_window", AsyncMock(return_value=_TEST_WINDOW)
    )
    monkeypatch.setattr(pool_mod, "read_refund_causes", AsyncMock(return_value={}))
    monkeypatch.setattr(
        pool_mod, "agentic_platform_cap_enforced", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        pool_mod, "platform_agentic_quota_is_unlimited", AsyncMock(return_value=False)
    )
    predicate = AsyncMock(return_value=False)
    monkeypatch.setattr(pool_mod, "has_enforceable_agentic_limit", predicate)
    _enable(monkeypatch, pool=2)

    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    redis.store[
        _usage_key(
            user_id, quota_module._POOL_USAGE_TYPE, None, _TEST_WINDOW.key, agent_id
        )
    ] = 2

    status = await pool_mod.get_pool_status(
        _StatusSession({str(agent_id): "agent"}, {str(user_id): "u@example.com"})
    )

    predicate.assert_not_awaited()
    assert status["platform_quota_unlimited"] is False
    (entry,) = status["entries"]
    assert entry["exhausted"] is True
    assert entry["quota_unlimited"] is False
    assert status["totals"]["exhausted"] == 1


def test_the_assignment_trace_writes_the_outcome_the_pool_reported():
    """``quota_refund_outcome: pool_exhausted`` on a run whose user has no
    quota is the same false alarm in the trace - and so is it on a run whose
    pool is switched off or whose refund path broke. The engine writes the
    outcome the helper reports instead of inferring one from ``False``, and
    "pool_exhausted" is only the fallback for a report that never arrived."""
    src = inspect.getsource(assignment_engine_mod.execute_assignment_run)
    assert "outcome_out=_pool_report" in src
    assert '_pool_report.get("outcome") or REFUND_OUTCOME_POOL_EXHAUSTED' in src
    # No collapse of the reported reasons into a literal: every
    # ``REFUND_OUTCOME_*`` the helper can report lands in the trace as is.
    assert '_refund_outcome = "pool_exhausted"' not in src
    assert '_refund_outcome = "quota_unlimited"' not in src


def test_the_chat_path_hands_the_pool_a_session():
    """The gate needs a session to answer; the chat refund passes its own as
    the resolution signal (the helper opens a private one from it), so a chat
    failure on an unlimited quota is not pooled either."""
    src = inspect.getsource(agent_chat_mod.run_agent_chat)
    refund_at = src.index("await refund_agentic_usage_for_failure(")
    call = src[refund_at : refund_at + 200]
    assert "db=db" in call


# ---------------------------------------------------------------------------
# The cause breakdown decomposes the same total the card shows
# ---------------------------------------------------------------------------


def _causes_by_kind(pool_causes: dict, free_causes: dict | None = None):
    async def _read(usage_type, _window_key, user_ids=()):
        if usage_type == quota_module._POOL_USAGE_TYPE:
            return dict(pool_causes)
        return dict(free_causes or {})

    return AsyncMock(side_effect=_read)


def _status_env(monkeypatch, *, pool: int, pool_causes: dict, free_causes=None):
    redis = _StatusRedis()
    monkeypatch.setattr(pool_mod, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(
        pool_mod, "resolve_pool_window", AsyncMock(return_value=_TEST_WINDOW)
    )
    monkeypatch.setattr(
        pool_mod, "read_refund_causes", _causes_by_kind(pool_causes, free_causes)
    )
    monkeypatch.setattr(
        pool_mod, "agentic_platform_cap_enforced", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        pool_mod, "platform_agentic_quota_is_unlimited", AsyncMock(return_value=False)
    )
    _enable(monkeypatch, pool=pool)
    return redis


def _pool_counter(redis, user_id, agent_id, used, usage_type=None):
    redis.store[
        _usage_key(
            user_id,
            usage_type or quota_module._POOL_USAGE_TYPE,
            None,
            _TEST_WINDOW.key,
            agent_id,
        )
    ] = used


async def test_credits_of_deleted_users_and_agents_are_reported_not_clamped(
    monkeypatch, caplog
):
    """The platform-wide tally keeps the causes of a pair whose user AND agent
    were deleted; the total used to drop them, the breakdown overshot and was
    clamped. The total keeps them now, and says how many are stale."""
    import logging

    redis = _status_env(
        monkeypatch, pool=5, pool_causes={"provider_overloaded": 5}, free_causes={"provider_unreachable": 1}
    )
    live_user, live_agent = uuid.uuid4(), uuid.uuid4()
    gone_user, gone_agent = uuid.uuid4(), uuid.uuid4()
    _pool_counter(redis, live_user, live_agent, 3)
    _pool_counter(redis, gone_user, gone_agent, 2)
    _pool_counter(redis, gone_user, gone_agent, 1, quota_module._FREE_REFUND_USAGE_TYPE)

    with caplog.at_level(logging.WARNING, logger="app.services.agent_failure_pool"):
        status = await pool_mod.get_pool_status(
            _StatusSession({str(live_agent): "agent"}, {str(live_user): "u@example.com"})
        )

    assert [e["agent_id"] for e in status["entries"]] == [str(live_agent)]
    totals = status["totals"]
    assert totals["failures_forgiven"] == 5
    assert totals["free_credits"] == 1
    assert totals["stale_pairs"] == 1
    assert totals["stale_failures_forgiven"] == 2
    assert totals["stale_free_credits"] == 1
    assert status["forgiven_by_cause"] == [{"cause": "provider_overloaded", "count": 5}]
    assert status["free_credits_by_cause"] == [{"cause": "provider_unreachable", "count": 1}]
    assert not [r for r in caplog.records if "tally exceeds" in r.getMessage()]


async def test_lowering_the_pool_size_mid_window_does_not_desync_the_breakdown(
    monkeypatch, caplog
):
    """Five credits granted under a pool of 5, then the operator lowers it to
    3: the credits were given, the tally holds five, so the total says five."""
    import logging

    redis = _status_env(monkeypatch, pool=3, pool_causes={"tool_error": 5})
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    _pool_counter(redis, user_id, agent_id, 5)

    with caplog.at_level(logging.WARNING, logger="app.services.agent_failure_pool"):
        status = await pool_mod.get_pool_status(
            _StatusSession({str(agent_id): "agent"}, {str(user_id): "u@example.com"})
        )

    assert status["totals"]["failures_forgiven"] == 5
    assert status["forgiven_by_cause"] == [{"cause": "tool_error", "count": 5}]
    (entry,) = status["entries"]
    assert entry["remaining"] == 0 and entry["exhausted"] is True
    assert not [r for r in caplog.records if "tally exceeds" in r.getMessage()]


async def test_the_recharge_clears_the_cause_breakdown(monkeypatch):
    """A redemption zeroes the counters AND drops the pool breakdown, inside
    the advisory lock the flush takes, so the card never shows a pre-recharge
    decomposition under a zeroed headline."""
    calls: list[str] = []
    session = _ResetSession()
    execute = session.execute

    async def _execute(stmt, params=None):
        calls.append("flush_lock" if "pg_try_advisory_xact_lock" in str(stmt) else "sql")
        return await execute(stmt, params)

    async def _clear(redis, usage_type, below_global_epoch=None):
        calls.append(f"clear_causes:{usage_type}")
        return 1

    session.execute = _execute
    monkeypatch.setattr(pool_mod, "redis_client", AsyncMock(return_value=_StatusRedis()))
    monkeypatch.setattr(pool_mod, "clear_refund_causes", _clear)

    await pool_mod._clear_pool_counters(session)

    assert calls[0] == "flush_lock"
    assert f"clear_causes:{quota_module._POOL_USAGE_TYPE}" in calls
    assert f"clear_causes:{quota_module._FREE_REFUND_USAGE_TYPE}" not in calls
    assert session.committed


# ---------------------------------------------------------------------------
# A quota reset takes the user's causes out with their counters
# ---------------------------------------------------------------------------


class _ResetRedis(_StatusRedis):
    """Counters AND cause hashes: the refund path writes both, the quota reset
    deletes both, and the card reads both."""

    async def hgetall(self, key):
        return {field: str(count) for field, count in self.hashes.get(key, {}).items()}

    def pipeline(self, transaction=True):
        return _HashPipeline(self)

    async def scan_iter(self, match="*", count=None):
        import fnmatch

        for key in [*self.store, *self.hashes]:
            if fnmatch.fnmatch(key, match):
                yield key

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            removed += self.store.pop(key, None) is not None
            removed += self.hashes.pop(key, None) is not None
        return removed


class _HashPipeline:
    def __init__(self, redis: _ResetRedis):
        self._redis = redis
        self._keys: list[str] = []

    def hgetall(self, key):
        self._keys.append(("hgetall", key))
        return self

    def get(self, key):
        self._keys.append(("get", key))
        return self

    async def execute(self):
        return [await getattr(self._redis, op)(key) for op, key in self._keys]


class _ResetSession:
    """What a quota reset or a pool recharge asks of its session: the flush
    lock (a try, granted unless *flush_running*), the pool-clear lock, the
    transaction id its stash is filed under, the DELETEs - the pool and free
    one returning what it removed - and a commit. No durable row unless a test
    hands some in - the counters live in Redis, as above. *xact_status* is how
    PostgreSQL answers for a stash's transaction, and *fail_commit* makes the
    commit fail. ``statements`` records what ran, in order."""

    def __init__(
        self,
        removed_rows=(),
        *,
        flush_running: bool = False,
        xact_status: str | None = "aborted",
        fail_commit: bool = False,
    ):
        self._removed_rows = list(removed_rows)
        self._flush_running = flush_running
        self._xact_status = xact_status
        self._fail_commit = fail_commit
        self.statements: list[str] = []
        self.committed = False
        self.rollbacks = 0

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.statements.append(sql)
        rows = self._removed_rows if "RETURNING" in sql else []
        if "pg_current_xact_id" in sql:
            value = "4242"
        elif "pg_xact_status" in sql:
            value = self._xact_status
        else:
            value = not (self._flush_running and "pg_try_advisory_xact_lock" in sql)

        class _Result:
            def all(self):
                return rows

            def scalar(self):
                return value

        return _Result()

    async def commit(self):
        if self._fail_commit:
            raise RuntimeError("the commit failed")
        self.committed = True

    async def rollback(self):
        self.rollbacks += 1


def _reset_env(monkeypatch) -> _ResetRedis:
    redis = _ResetRedis()
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(pool_mod, "redis_client", AsyncMock(return_value=redis))
    _enable(monkeypatch, pool=5)
    _private_session(monkeypatch)
    monkeypatch.setattr(
        quota_module, "has_enforceable_agentic_limit", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        pool_mod, "agentic_platform_cap_enforced", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        pool_mod, "platform_agentic_quota_is_unlimited", AsyncMock(return_value=False)
    )
    return redis


async def _fail_runs(user_id, agent_id, cause, runs, *, free=False):
    refund = (
        refund_agentic_usage_for_provider_outage
        if free
        else refund_agentic_usage_for_failure
    )
    for _ in range(runs):
        await increment_agentic_usage(user_id, agent_id=agent_id)
        assert await refund(user_id, agent_id, db=object(), cause=cause)


async def test_a_quota_reset_takes_the_users_causes_out_of_the_breakdown(
    monkeypatch, caplog
):
    """An admin's "Reset usage" deletes the user's pool and free counters, so
    the card's totals lose that user's credits. Their causes must go with
    them: left in the tally, the breakdown decomposes a total smaller than
    itself for the rest of the window."""
    import logging

    _reset_env(monkeypatch)
    kept, reset, agent_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _fail_runs(kept, agent_id, "tool_error", 3)
    await _fail_runs(reset, agent_id, "provider_overloaded", 2)
    await _fail_runs(reset, agent_id, "provider_unreachable", 1, free=True)
    session = _StatusSession(
        {str(agent_id): "agent"},
        {str(kept): "kept@example.com", str(reset): "reset@example.com"},
    )

    before = await pool_mod.get_pool_status(session)
    assert before["totals"]["failures_forgiven"] == 5
    assert before["forgiven_by_cause"] == [
        {"cause": "tool_error", "count": 3},
        {"cause": "provider_overloaded", "count": 2},
    ]
    assert before["free_credits_by_cause"] == [
        {"cause": "provider_unreachable", "count": 1}
    ]

    await quota_module.reset_user_quota_usage(_ResetSession(), reset)

    with caplog.at_level(logging.WARNING, logger="app.services.agent_failure_pool"):
        after = await pool_mod.get_pool_status(session)
    assert after["totals"]["failures_forgiven"] == 3
    assert after["totals"]["free_credits"] == 0
    assert after["forgiven_by_cause"] == [{"cause": "tool_error", "count": 3}]
    assert after["free_credits_by_cause"] == []
    assert not [r for r in caplog.records if "tally exceeds" in r.getMessage()]


async def test_a_reset_drops_the_platform_wide_tally_it_can_no_longer_vouch_for(
    monkeypatch, caplog
):
    """Causes tallied before they were tallied per user sit in one platform-wide
    hash that cannot say whose they were. A reset that removes credits from a
    window drops that window's hash - its credits then read ``not_recorded``,
    which is true - and a reset of someone with no credit there keeps it."""
    import logging

    redis = _reset_env(monkeypatch)
    kept, reset, idle, agent_id = (uuid.uuid4() for _ in range(4))
    _pool_counter(redis, kept, agent_id, 2)
    _pool_counter(redis, reset, agent_id, 2)
    pool = quota_module._POOL_USAGE_TYPE
    legacy = quota_module._refund_cause_key(pool, _TEST_WINDOW.key)
    older = quota_module._refund_cause_key(pool, "2025-01-01")
    redis.hashes[legacy] = {"unknown": 4}
    redis.hashes[older] = {"unknown": 9}
    session = _StatusSession(
        {str(agent_id): "agent"},
        {str(kept): "kept@example.com", str(reset): "reset@example.com"},
    )

    await quota_module.reset_user_quota_usage(_ResetSession(), idle)
    status = await pool_mod.get_pool_status(session)
    assert status["forgiven_by_cause"] == [{"cause": "unknown", "count": 4}]

    await quota_module.reset_user_quota_usage(_ResetSession(), reset)
    with caplog.at_level(logging.WARNING, logger="app.services.agent_failure_pool"):
        status = await pool_mod.get_pool_status(session)
    assert status["totals"]["failures_forgiven"] == 2
    assert status["forgiven_by_cause"] == [{"cause": "not_recorded", "count": 2}]
    assert legacy not in redis.hashes
    assert redis.hashes[older] == {"unknown": 9}, "nothing was removed from it"
    assert not [r for r in caplog.records if "tally exceeds" in r.getMessage()]


async def test_a_reset_reads_the_windows_off_the_rows_it_deletes(monkeypatch):
    """A counter can live in the DB alone (Redis lost it after the flush), so
    the windows a reset removed credits from are read off the deleted rows
    too, not only off the Redis keys."""
    from types import SimpleNamespace

    redis = _reset_env(monkeypatch)
    free = quota_module._FREE_REFUND_USAGE_TYPE
    legacy = quota_module._refund_cause_key(free, _TEST_WINDOW.key)
    redis.hashes[legacy] = {"provider_unreachable": 1}
    removed = [
        SimpleNamespace(usage_type=free, period=_TEST_WINDOW.key, agent_id=None, count=1)
    ]

    await quota_module.reset_user_quota_usage(_ResetSession(removed), uuid.uuid4())

    assert legacy not in redis.hashes


async def test_a_refund_in_flight_across_a_reset_leaves_no_stray_cause(
    monkeypatch, caplog
):
    """The reset lands between a refund's claim and its tally, while the refund
    is still reversing the usage counters. The counter the claim went into is
    gone, so the cause must not be written after it: a stray cause would
    overshoot the total the next time the user is credited back."""
    import logging

    redis = _reset_env(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    real_refund = quota_module.refund_agentic_usage

    async def _refund_during_a_reset(*args, **kwargs):
        await quota_module.reset_user_quota_usage(_ResetSession(), user_id)
        return await real_refund(*args, **kwargs)

    monkeypatch.setattr(quota_module, "refund_agentic_usage", _refund_during_a_reset)
    await _fail_runs(user_id, agent_id, "tool_error", 1)
    monkeypatch.setattr(quota_module, "refund_agentic_usage", real_refund)
    assert not [key for key in redis.hashes if key.endswith(str(user_id))]

    await _fail_runs(user_id, agent_id, "provider_overloaded", 1)
    with caplog.at_level(logging.WARNING, logger="app.services.agent_failure_pool"):
        status = await pool_mod.get_pool_status(
            _StatusSession({str(agent_id): "agent"}, {str(user_id): "u@example.com"})
        )
    assert status["totals"]["failures_forgiven"] == 1
    assert status["forgiven_by_cause"] == [{"cause": "provider_overloaded", "count": 1}]
    assert not [r for r in caplog.records if "tally exceeds" in r.getMessage()]


async def test_a_refund_in_flight_across_a_recharge_leaves_no_stray_cause(
    monkeypatch, caplog
):
    """The same race with the other admin action: a support-code recharge
    sweeps every pool counter, then every pool cause, while a refund sits
    between its claim and its tally. The tally finds its counter gone and
    writes nothing."""
    import logging

    redis = _reset_env(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    real_refund = quota_module.refund_agentic_usage

    async def _refund_during_a_recharge(*args, **kwargs):
        await pool_mod._clear_pool_counters(_ResetSession())
        return await real_refund(*args, **kwargs)

    monkeypatch.setattr(quota_module, "refund_agentic_usage", _refund_during_a_recharge)
    await _fail_runs(user_id, agent_id, "tool_error", 1)
    monkeypatch.setattr(quota_module, "refund_agentic_usage", real_refund)
    assert redis.hashes == {}

    await _fail_runs(user_id, agent_id, "provider_overloaded", 1)
    with caplog.at_level(logging.WARNING, logger="app.services.agent_failure_pool"):
        status = await pool_mod.get_pool_status(
            _StatusSession({str(agent_id): "agent"}, {str(user_id): "u@example.com"})
        )
    assert status["totals"]["failures_forgiven"] == 1
    assert status["forgiven_by_cause"] == [{"cause": "provider_overloaded", "count": 1}]
    assert not [r for r in caplog.records if "tally exceeds" in r.getMessage()]


# ---------------------------------------------------------------------------
# Races around the claim and the admin actions
# ---------------------------------------------------------------------------


async def test_two_cold_claims_cannot_grant_more_than_the_pool(monkeypatch):
    """Redis lost the pool counter; the durable row says the pool is spent.
    Two failures of the same pair claim together: the first reads the cold
    key and goes to the database for the baseline, and the second must not be
    granted in the meantime off a counter the baseline was never folded into."""
    import asyncio

    redis = _fake_redis(monkeypatch)
    _enable(monkeypatch, pool=3)
    _private_session(monkeypatch)
    monkeypatch.setattr(
        quota_module, "has_enforceable_agentic_limit", AsyncMock(return_value=True)
    )

    async def _spent_pool(*_args):
        await asyncio.sleep(0)  # the database round trip: the other claim runs
        return 3

    monkeypatch.setattr(quota_module, "_load_pool_baseline", _spent_pool)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()

    granted = await asyncio.gather(
        *(
            refund_agentic_usage_for_failure(user_id, agent_id, db=object())
            for _ in range(2)
        )
    )

    assert granted == [False, False]
    assert redis.store[_failure_pool_key(user_id, agent_id, _TEST_WINDOW.key)] == 3


async def test_a_cold_claim_under_the_pool_still_counts_from_the_baseline(monkeypatch):
    """The other half: with room left, cold claims are granted until the
    durable baseline plus the new claims reach the pool, and not one more."""
    import asyncio

    redis = _fake_redis(monkeypatch)
    _enable(monkeypatch, pool=3)
    _private_session(monkeypatch)
    monkeypatch.setattr(
        quota_module, "has_enforceable_agentic_limit", AsyncMock(return_value=True)
    )

    async def _one_spent(*_args):
        await asyncio.sleep(0)
        return 1

    monkeypatch.setattr(quota_module, "_load_pool_baseline", _one_spent)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()

    granted = await asyncio.gather(
        *(
            refund_agentic_usage_for_failure(user_id, agent_id, db=object())
            for _ in range(4)
        )
    )

    assert sorted(granted) == [False, False, True, True]
    assert redis.store[_failure_pool_key(user_id, agent_id, _TEST_WINDOW.key)] == 3


async def test_a_stale_tally_cannot_come_back_through_a_new_claim_on_the_pair(
    monkeypatch, caplog
):
    """Two failures of one pair straddle a reset: the first is between its
    claim and its tally when the reset lands, the second claims the same pair
    right after it and takes the fresh counter back up to the first one's
    value. The first tally must still not be read: its credit's counter is
    the one the reset deleted."""
    import logging

    _reset_env(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    real_refund = quota_module.refund_agentic_usage
    straddled = False

    async def _refund_across_a_reset(*args, **kwargs):
        nonlocal straddled
        if not straddled:
            straddled = True
            await quota_module.reset_user_quota_usage(_ResetSession(), user_id)
            await _fail_runs(user_id, agent_id, "provider_overloaded", 1)
        return await real_refund(*args, **kwargs)

    monkeypatch.setattr(quota_module, "refund_agentic_usage", _refund_across_a_reset)
    await _fail_runs(user_id, agent_id, "tool_error", 1)

    with caplog.at_level(logging.WARNING, logger="app.services.agent_failure_pool"):
        status = await pool_mod.get_pool_status(
            _StatusSession({str(agent_id): "agent"}, {str(user_id): "u@example.com"})
        )
    assert status["totals"]["failures_forgiven"] == 1
    assert status["forgiven_by_cause"] == [{"cause": "provider_overloaded", "count": 1}]
    assert not [r for r in caplog.records if "tally exceeds" in r.getMessage()]


async def test_a_stale_tally_cannot_come_back_across_a_recharge(monkeypatch, caplog):
    """The same straddle across a support-code recharge."""
    import logging

    _reset_env(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    real_refund = quota_module.refund_agentic_usage
    straddled = False

    async def _refund_across_a_recharge(*args, **kwargs):
        nonlocal straddled
        if not straddled:
            straddled = True
            await pool_mod._clear_pool_counters(_ResetSession())
            await _fail_runs(user_id, agent_id, "provider_overloaded", 1)
        return await real_refund(*args, **kwargs)

    monkeypatch.setattr(quota_module, "refund_agentic_usage", _refund_across_a_recharge)
    await _fail_runs(user_id, agent_id, "tool_error", 1)

    with caplog.at_level(logging.WARNING, logger="app.services.agent_failure_pool"):
        status = await pool_mod.get_pool_status(
            _StatusSession({str(agent_id): "agent"}, {str(user_id): "u@example.com"})
        )
    assert status["totals"]["failures_forgiven"] == 1
    assert status["forgiven_by_cause"] == [{"cause": "provider_overloaded", "count": 1}]
    assert not [r for r in caplog.records if "tally exceeds" in r.getMessage()]


async def test_tallies_written_before_any_reset_are_still_read(monkeypatch):
    """Epochs start at zero, and a zero epoch files a tally under the
    per-user key with no epoch: nothing written before epochs goes missing."""
    redis = _reset_env(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    _pool_counter(redis, user_id, agent_id, 2)
    pool = quota_module._POOL_USAGE_TYPE
    redis.hashes[quota_module._refund_cause_key(pool, _TEST_WINDOW.key, user_id)] = {
        "tool_error": 2
    }

    status = await pool_mod.get_pool_status(
        _StatusSession({str(agent_id): "agent"}, {str(user_id): "u@example.com"})
    )

    assert status["forgiven_by_cause"] == [{"cause": "tool_error", "count": 2}]


def test_the_card_reads_the_tally_of_the_users_behind_each_total():
    """The population filter is what keeps the breakdown and the totals on the
    same people; a read of every user's tally would bring the overshoot back."""
    src = inspect.getsource(pool_mod.get_pool_status)
    assert "user_ids={user for user, _agent in counts}" in src
    assert "user_ids={user for user, _agent in free_counts}" in src


async def test_a_reset_holds_both_locks_before_it_deletes_a_counter(monkeypatch):
    """The flush lock keeps a flush from writing back what the reset deletes,
    the user's pool-clear lock keeps a cold claim from seeding back a row it
    deletes: both are held before the first counter or row goes."""
    redis = _reset_env(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    _pool_counter(redis, user_id, agent_id, 2)
    session = _ResetSession()
    params: list = []
    record = session.execute

    async def _execute(stmt, bind=None):
        params.append(bind)
        return await record(stmt, bind)

    async def _delete(*keys):
        session.statements.append("redis DEL")
        params.append(None)
        return await _ResetRedis.delete(redis, *keys)

    session.execute = _execute
    redis.delete = _delete

    await quota_module.reset_user_quota_usage(session, user_id)

    ran = session.statements
    flush_lock = next(i for i, s in enumerate(ran) if "pg_try_advisory_xact_lock(" in s)
    pool_clear = next(i for i, s in enumerate(ran) if "hashtext(:scope)" in s)
    first_delete = next(i for i, s in enumerate(ran) if "DEL" in s)
    assert flush_lock < pool_clear < first_delete
    assert params[pool_clear]["scope"] == quota_module._pool_clear_scope(user_id)
    assert _failure_pool_key(user_id, agent_id, _TEST_WINDOW.key) not in redis.store


async def test_a_reset_behind_a_flush_that_keeps_the_lock_clears_nothing(monkeypatch):
    """A flush still holding its lock after the bounded wait may have read the
    user's counters already: a reset going ahead without the lock let it write
    them back, leaving the pool spent. The reset asks the flush to
    stand aside while it waits, then refuses before it clears anything.

    The wait is zero: the deadline has passed at the first try, so the second
    try and the yield request are the guarantee itself, never a race against
    the runner's clock."""
    import asyncio

    redis = _reset_env(monkeypatch)
    monkeypatch.setattr(quota_module, "_RESET_FLUSH_LOCK_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(quota_module, "_RESET_FLUSH_LOCK_POLL_SECONDS", 0.0)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    _pool_counter(redis, user_id, agent_id, 2)
    session = _ResetSession(flush_running=True)

    with pytest.raises(quota_module.QuotaClearBusyError):
        await asyncio.wait_for(
            quota_module.reset_user_quota_usage(session, user_id), timeout=5
        )

    tries = [s for s in session.statements if "pg_try_advisory_xact_lock(" in s]
    assert len(tries) == 2, "the reset gave up without waiting for the flush"
    assert not [s for s in session.statements if "DELETE" in s]
    assert redis.store[_failure_pool_key(user_id, agent_id, _TEST_WINDOW.key)] == 2
    assert quota_module._FLUSH_YIELD_KEY in redis.store, "the flush was not asked to yield"


async def test_the_recharge_behind_a_flush_that_keeps_the_lock_clears_nothing(
    monkeypatch,
):
    """The same bound for a support-code recharge, which used to wait in a
    blocking lock the statement timeout then cancelled. Refused before anything
    is cleared, the code is not burned."""
    import asyncio

    redis = _reset_env(monkeypatch)
    monkeypatch.setattr(quota_module, "_RESET_FLUSH_LOCK_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(quota_module, "_RESET_FLUSH_LOCK_POLL_SECONDS", 0.01)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    _pool_counter(redis, user_id, agent_id, 2)
    session = _ResetSession(flush_running=True)

    with pytest.raises(quota_module.QuotaClearBusyError):
        await asyncio.wait_for(pool_mod._clear_pool_counters(session), timeout=5)

    assert not [s for s in session.statements if "DELETE" in s]
    assert not session.committed
    assert redis.store[_failure_pool_key(user_id, agent_id, _TEST_WINDOW.key)] == 2


async def test_the_recharge_holds_the_platform_pool_clear_lock_before_it_deletes(
    monkeypatch,
):
    """Every pool is cleared, so every cold claim must wait for the recharge
    to end: the lock is the platform's, taken before the rows and the keys
    go."""
    calls: list[tuple[str, dict | None]] = []
    session = _ResetSession()
    execute = session.execute

    async def _execute(stmt, params=None):
        calls.append((str(stmt), params))
        return await execute(stmt, params)

    session.execute = _execute
    monkeypatch.setattr(pool_mod, "redis_client", AsyncMock(return_value=_StatusRedis()))

    await pool_mod._clear_pool_counters(session)

    ran = [sql for sql, _params in calls]
    pool_clear = next(i for i, s in enumerate(ran) if "hashtext(:scope)" in s)
    assert pool_clear < next(i for i, s in enumerate(ran) if "DELETE" in s)
    assert calls[pool_clear][1]["scope"] == quota_module._pool_clear_scope(
        quota_module._GLOBAL_REFUND_EPOCH
    )


# ---------------------------------------------------------------------------
# A reset or a recharge that fails loses no increment
# ---------------------------------------------------------------------------


def _usage_counter(redis, user_id, count: int) -> str:
    key = _usage_key(user_id, "agentic", None, _current_period_key("monthly"))
    redis.store[key] = count
    return key


async def test_a_reset_that_fails_puts_its_counters_back(monkeypatch):
    """The reset's commit fails after its counters left Redis. Deleted, the
    increments only Redis held were gone with them - a whole spent pool here,
    never flushed - so the next failure was decided against the durable row
    and granted past a spent pool. They come back."""
    redis = _reset_env(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    _pool_counter(redis, user_id, agent_id, 5)
    usage = _usage_counter(redis, user_id, 7)
    session = _ResetSession(fail_commit=True)

    with pytest.raises(RuntimeError, match="the commit failed"):
        await quota_module.reset_user_quota_usage(session, user_id)

    assert redis.store[_failure_pool_key(user_id, agent_id, _TEST_WINDOW.key)] == 5
    assert redis.store[usage] == 7
    assert quota_module._CLEAR_STASH_INDEX not in redis.hashes
    assert session.rollbacks
    assert not await refund_agentic_usage_for_failure(user_id, agent_id, db=object())


async def test_a_recharge_that_fails_puts_every_pool_back(monkeypatch):
    """The same for the platform-wide clear, which commits the redemption with
    the recharge: neither lands, and no pool is found refilled."""
    redis = _reset_env(monkeypatch)
    first, second, agent_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _pool_counter(redis, first, agent_id, 3)
    _pool_counter(redis, second, agent_id, 1)

    with pytest.raises(RuntimeError, match="the commit failed"):
        await pool_mod._clear_pool_counters(_ResetSession(fail_commit=True))

    assert redis.store[_failure_pool_key(first, agent_id, _TEST_WINDOW.key)] == 3
    assert redis.store[_failure_pool_key(second, agent_id, _TEST_WINDOW.key)] == 1
    assert quota_module._CLEAR_STASH_INDEX not in redis.hashes


async def test_a_counter_seeded_after_a_failed_clear_is_put_back_on_top_of_its_seed(
    monkeypatch,
):
    """A failed clear's locks are gone before its counters are back: a cold
    claim meanwhile seeds the key from the restored durable row. Putting the
    stashed count back on top of it must not count that row twice."""
    from types import SimpleNamespace

    redis = _reset_env(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    key = _failure_pool_key(user_id, agent_id, _TEST_WINDOW.key)
    redis.store[key] = 3
    removed = [
        SimpleNamespace(
            usage_type=quota_module._POOL_USAGE_TYPE,
            period=_TEST_WINDOW.key,
            agent_id=agent_id,
            count=1,
        )
    ]

    class _ClaimMeanwhile(_ResetSession):
        async def rollback(self):
            await super().rollback()
            if self.rollbacks == 1:
                # The durable row is back at 1; a claim seeds from it.
                await quota_module._claim_pool_slot(redis, key, user_id, 5, 1)

    with pytest.raises(RuntimeError):
        await quota_module.reset_user_quota_usage(
            _ClaimMeanwhile(removed, fail_commit=True), user_id
        )

    assert redis.store[key] == 4, "three before the reset, one claimed after"


async def test_a_committed_clear_leaves_no_stash_and_nothing_to_put_back(monkeypatch):
    redis = _reset_env(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    _pool_counter(redis, user_id, agent_id, 3)

    await quota_module.reset_user_quota_usage(_ResetSession(), user_id)

    assert _failure_pool_key(user_id, agent_id, _TEST_WINDOW.key) not in redis.store
    assert not [key for key in redis.hashes if "clear_stash" in key]


@pytest.mark.parametrize(
    ("status", "restored", "kept"),
    [
        ("aborted", True, False),
        # Older than PostgreSQL still records: nothing may be lost.
        (None, True, False),
        ("committed", False, False),
        ("in progress", False, True),
    ],
)
async def test_a_stash_is_settled_by_how_its_transaction_ended(
    monkeypatch, status, restored, kept
):
    """A clear whose process died leaves its stash behind. Whoever next holds
    the flush lock settles it from PostgreSQL's record of that transaction:
    put back unless it committed, left alone while it is still running."""
    redis = _reset_env(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    key = _failure_pool_key(user_id, agent_id, _TEST_WINDOW.key)
    redis.store[key] = 3
    token = await quota_module._open_clear_stash(redis, _ResetSession())
    await quota_module._stash_counters(redis, token, [key], {})

    await quota_module._settle_clear_stashes(redis, _ResetSession(xact_status=status))

    assert (redis.store.get(key) == 3) is restored
    assert (quota_module._CLEAR_STASH_INDEX in redis.hashes) is kept


async def test_a_counter_moved_twice_counts_its_seed_once(monkeypatch):
    """A counter moved, seeded again from its durable row and moved a second
    time: the seed is in the first count already, so the stash adds only what
    the counter gained over it."""
    redis = _reset_env(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    key = _failure_pool_key(user_id, agent_id, _TEST_WINDOW.key)
    token = await quota_module._open_clear_stash(redis, _ResetSession())
    redis.store[key] = 3
    await quota_module._stash_counters(redis, token, [key], {key: 1})
    redis.store[key] = 2  # the durable 1, plus one claim
    await quota_module._stash_counters(redis, token, [key], {key: 1})

    await quota_module._settle_clear_stashes(redis, _ResetSession())

    assert redis.store[key] == 4


async def test_a_clear_puts_an_earlier_clears_stash_back_before_it_clears(monkeypatch):
    """An earlier reset of the same user died before it committed. A later
    reset settles that stash first and then clears what it put back: settled
    afterwards, it would bring the counters back over the later reset."""
    redis = _reset_env(monkeypatch)
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    key = _failure_pool_key(user_id, agent_id, _TEST_WINDOW.key)
    redis.store[key] = 3
    token = await quota_module._open_clear_stash(redis, _ResetSession())
    await quota_module._stash_counters(redis, token, [key], {})

    await quota_module.reset_user_quota_usage(_ResetSession(), user_id)
    await quota_module._settle_clear_stashes(redis, _ResetSession())

    assert key not in redis.store
    assert quota_module._CLEAR_STASH_INDEX not in redis.hashes
