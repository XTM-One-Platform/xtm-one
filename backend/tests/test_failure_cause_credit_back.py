"""The credit-back must reach the trace, and carry its cause.

Two things the classifier made possible once the agent failure pool
landed:

* a run that is charged and then credited back must not keep ``"charged"`` in
  its trace forever — the Trace Explorer stating the opposite of what happened;
* the two headline credit-back totals in Settings -> Quotas must decompose by
  what was actually forgiven, instead of being two unexplained numbers.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.services.agent_failure_pool import _cause_breakdown
from app.services.failure_causes import FailureCause
from tests.ast_helpers import call_lines, function_node

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ENGINE = BACKEND_ROOT / "app/services/assignment_engine.py"


# ── Ordering: the correction only works if the refund happens first ──────────


def test_the_refund_runs_before_the_trace_is_closed():
    """The whole fix is an ordering, so the ordering is what gets pinned.

    ``end_trace`` is what assembles and flushes the durable local trace. A
    credit-back granted after it lands nowhere — which is exactly the bug
    once documented as unfixable ("shows up in the logs,
    not on the trace"). It is fixable: Langfuse's root metadata is immutable
    after creation, but ours is only built at ``end_trace``, so the correction
    merely has to be known by then.

    A future edit that moves either call past the other reintroduces a silent
    wrong number on every credited-back run, with nothing failing.
    """
    node = function_node(ENGINE, "execute_assignment_run")

    refunds = call_lines(node, "refund_agentic_usage_for_provider_outage")
    refunds += call_lines(node, "refund_agentic_usage_for_failure")
    ends = call_lines(node, "end_trace")
    assert refunds, "expected the failure handler to credit the action back"
    assert ends, "expected the failure handler to close the trace"

    # The failure handler's ``end_trace`` is the LAST one in the function (the
    # success and HITL paths close theirs earlier), so compare against that.
    assert max(refunds) < max(ends), (
        "the credit-back must be applied before end_trace flushes the trace, "
        "or the trace keeps asserting a charge that was reversed"
    )


def test_the_corrected_quota_metadata_is_pushed_onto_the_trace():
    """Pins the mechanism, not just the ordering: the handler must actually
    enrich the trace metadata rather than only log the refund."""
    source = ENGINE.read_text(encoding="utf-8")
    assert "quota_refund_outcome" in source
    assert "credited_back_free" in source
    assert "credited_back_from_pool" in source


def test_the_cause_is_handed_to_both_refund_paths():
    """A credit-back that does not carry its cause cannot be broken down."""
    source = ENGINE.read_text(encoding="utf-8")
    assert source.count("cause=_failure.cause.value") == 2


# ── The breakdown reconciles with the total it decomposes ────────────────────


def test_breakdown_is_ordered_by_size():
    result = _cause_breakdown({"a": 2, "b": 9, "c": 5}, 16)
    assert [item["cause"] for item in result] == ["b", "c", "a"]


def test_breakdown_ties_break_alphabetically():
    """Stable order, so the card does not reshuffle between refreshes."""
    result = _cause_breakdown({"b": 3, "a": 3}, 6)
    assert [item["cause"] for item in result] == ["a", "b"]


def test_a_shortfall_becomes_an_explicit_remainder():
    """Credits granted before this shipped — or lost with the Redis hash —
    must show as unrecorded, never silently shrink the picture."""
    result = _cause_breakdown({"provider_overloaded": 4}, 10)
    assert result[-1] == {"cause": "not_recorded", "count": 6}
    assert sum(item["count"] for item in result) == 10


def test_the_remainder_is_not_called_unknown():
    """``unknown`` is a real classification ("we looked and could not tell").
    "never tallied" is a different finding and must not borrow its name — the
    same distinction the trace filter draws between unknown and unrecorded."""
    result = _cause_breakdown({}, 3)
    assert result == [{"cause": "not_recorded", "count": 3}]
    assert FailureCause.UNKNOWN.value not in {item["cause"] for item in result}


def test_an_overshoot_is_clamped_rather_than_shown_negative():
    """The tally and the totals are reconciled from different places, so the
    breakdown can transiently run ahead of a total that has not flushed yet.
    The parts are capped at the headline they decompose, the smallest causes
    trimmed first, so the card never shows more causes than credits."""
    assert _cause_breakdown({"provider_overloaded": 9}, 4) == [
        {"cause": "provider_overloaded", "count": 4}
    ]
    assert _cause_breakdown({"a": 5, "b": 3, "c": 1}, 6) == [
        {"cause": "a", "count": 5},
        {"cause": "b", "count": 1},
    ]
    assert _cause_breakdown({"a": 2}, 0) == []


def test_zero_counts_are_dropped():
    result = _cause_breakdown({"a": 0, "b": 2}, 2)
    assert result == [{"cause": "b", "count": 2}]


def test_an_empty_tally_and_an_empty_total_render_nothing():
    assert _cause_breakdown({}, 0) == []


# ── The per-cause tally itself ───────────────────────────────────────────────


class _FakeRedis:
    def __init__(self, *, fail: bool = False):
        self.store: dict[str, int] = {}
        self.hashes: dict[str, dict[str, int]] = {}
        self.expires: list[str] = []
        self.deletes: list[tuple[str, ...]] = []
        self.fail = fail

    async def eval(self, script, numkeys, *keys_and_args):
        from app.services.quota import _RECORD_REFUND_CAUSE_LUA

        if self.fail:
            raise RuntimeError("redis is down")
        assert script == _RECORD_REFUND_CAUSE_LUA
        counter, cause_key, granted_at, cause, ttl = keys_and_args
        if int(self.store.get(counter, 0)) < int(granted_at):
            return 0
        await self.hincrby(cause_key, cause, 1)
        await self.expire(cause_key, ttl)
        return 1

    async def hincrby(self, key, field, amount):
        if self.fail:
            raise RuntimeError("redis is down")
        self.hashes.setdefault(key, {})
        self.hashes[key][field] = self.hashes[key].get(field, 0) + amount
        return self.hashes[key][field]

    async def expire(self, key, ttl):
        self.expires.append(key)

    async def hgetall(self, key):
        if self.fail:
            raise RuntimeError("redis is down")
        return {k: str(v) for k, v in self.hashes.get(key, {}).items()}

    async def get(self, key):
        if self.fail:
            raise RuntimeError("redis is down")
        value = self.store.get(key)
        return None if value is None else str(value)

    async def incr(self, key):
        if self.fail:
            raise RuntimeError("redis is down")
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    def pipeline(self, transaction=True):
        return _FakePipeline(self)

    async def scan_iter(self, match="*", count=None):
        import fnmatch

        if self.fail:
            raise RuntimeError("redis is down")
        for key in list(self.hashes):
            if fnmatch.fnmatch(key, match):
                yield key

    async def delete(self, *keys):
        self.deletes.append(keys)
        for key in keys:
            self.hashes.pop(key, None)
        return len(keys)


class _FakePipeline:
    def __init__(self, redis: _FakeRedis):
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


_USER = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_OTHER = uuid.UUID("00000000-0000-0000-0000-00000000000b")


_POOL_COUNTER = "quota:usage:user:agentic_failure_pool:_total:2026:agent"
_FREE_COUNTER = "quota:usage:user:agentic_free_refund:_total:2026:agent"


@pytest.mark.asyncio
async def test_a_cause_is_tallied_under_its_window_kind_and_user():
    from app.services.quota import (
        _record_refund_cause,
        _refund_cause_key,
    )

    redis = _FakeRedis()
    redis.store.update({_POOL_COUNTER: 2, _FREE_COUNTER: 1})
    for granted_at in (1, 2):
        await _record_refund_cause(
            redis,
            "agentic_failure_pool",
            "2026",
            "provider_overloaded",
            user_id=_USER,
            counter_key=_POOL_COUNTER,
            granted_at=granted_at,
        )
    await _record_refund_cause(
        redis,
        "agentic_free_refund",
        "2026",
        "failed_before_model_call",
        user_id=_USER,
        counter_key=_FREE_COUNTER,
        granted_at=1,
    )

    pool_key = _refund_cause_key("agentic_failure_pool", "2026", _USER)
    free_key = _refund_cause_key("agentic_free_refund", "2026", _USER)
    assert redis.hashes == {
        pool_key: {"provider_overloaded": 2},
        free_key: {"failed_before_model_call": 1},
    }, "per user only - the platform-wide hash is never written again"
    # The breakdown must not outlive its window.
    assert pool_key in redis.expires


@pytest.mark.asyncio
async def test_a_cause_whose_counter_was_reset_is_not_tallied():
    """A quota reset can land between a refund's claim and its tally. The
    counter the claim went into is then gone (or restarted below it), and a
    cause written anyway would count a credit no total counts any more."""
    from app.services.quota import _record_refund_cause

    redis = _FakeRedis()
    await _record_refund_cause(
        redis,
        "agentic_failure_pool",
        "2026",
        "tool_error",
        user_id=_USER,
        counter_key=_POOL_COUNTER,
        granted_at=1,
    )
    redis.store[_POOL_COUNTER] = 1
    await _record_refund_cause(
        redis,
        "agentic_failure_pool",
        "2026",
        "tool_error",
        user_id=_USER,
        counter_key=_POOL_COUNTER,
        granted_at=3,
    )
    assert redis.hashes == {}


@pytest.mark.asyncio
async def test_a_missing_cause_tallies_nothing():
    from app.services.quota import _record_refund_cause

    redis = _FakeRedis()
    redis.store[_POOL_COUNTER] = 1
    await _record_refund_cause(
        redis,
        "agentic_failure_pool",
        "2026",
        None,
        user_id=_USER,
        counter_key=_POOL_COUNTER,
        granted_at=1,
    )
    assert redis.hashes == {}


@pytest.mark.asyncio
async def test_a_tally_failure_never_propagates():
    """The refund has already been granted by this point. Losing the
    bookkeeping is strictly better than raising through a caller that is
    already handling a failure."""
    from app.services.quota import _record_refund_cause

    await _record_refund_cause(
        _FakeRedis(fail=True),
        "agentic_failure_pool",
        "2026",
        "unknown",
        user_id=_USER,
        counter_key=_POOL_COUNTER,
        granted_at=1,
    )


@pytest.mark.asyncio
async def test_the_breakdown_sums_the_users_it_is_asked_about(monkeypatch):
    """The card asks for the users whose counters make the total, so a user
    whose counters are gone (a quota reset) takes their causes out with them;
    the platform-wide hash written before causes were tallied per user is
    still read."""
    from app.services import quota as quota_mod

    redis = _FakeRedis()
    key = quota_mod._refund_cause_key
    redis.hashes[key("agentic_failure_pool", "2026")] = {"unknown": 1}
    redis.hashes[key("agentic_failure_pool", "2026", _USER)] = {"unknown": 2, "tool_error": 1}
    redis.hashes[key("agentic_failure_pool", "2026", _OTHER)] = {"tool_error": 5}
    redis.hashes[key("agentic_failure_pool", "2025", _USER)] = {"tool_error": 7}
    redis.hashes[key("agentic_free_refund", "2026", _USER)] = {"provider_unreachable": 3}

    async def _redis():
        return redis

    monkeypatch.setattr(quota_mod, "redis_client", _redis)
    assert await quota_mod.read_refund_causes(
        "agentic_failure_pool", "2026", user_ids=[str(_USER)]
    ) == {"unknown": 3, "tool_error": 1}
    assert await quota_mod.read_refund_causes("agentic_failure_pool", "2026") == {
        "unknown": 1
    }


@pytest.mark.asyncio
async def test_the_breakdown_read_is_batched(monkeypatch):
    """One hash per credited user: read through pipelines of a bounded size,
    never one command per user and never one unbounded command."""
    from app.services import quota as quota_mod

    redis = _FakeRedis()
    users = [uuid.uuid4() for _ in range(5)]
    for user in users:
        redis.hashes[quota_mod._refund_cause_key("agentic_free_refund", "2026", user)] = {
            "provider_unreachable": 1
        }
    batches: list[int] = []

    class _CountingPipeline(_FakePipeline):
        async def execute(self):
            batches.append(len(self._keys))
            return await super().execute()

    redis.pipeline = lambda transaction=True: _CountingPipeline(redis)

    async def _redis():
        return redis

    monkeypatch.setattr(quota_mod, "redis_client", _redis)
    monkeypatch.setattr(quota_mod, "_CAUSE_KEY_BATCH", 2)
    assert await quota_mod.read_refund_causes(
        "agentic_free_refund", "2026", user_ids=users
    ) == {"provider_unreachable": 5}
    # The epochs (the platform's plus five users'), then the tallies (the
    # platform-wide one plus five users'), two at a time.
    assert batches == [2, 2, 2, 2, 2, 2]


@pytest.mark.asyncio
async def test_reading_a_broken_breakdown_reports_nothing_rather_than_failing(monkeypatch):
    """A card that cannot read the breakdown still renders its totals — the
    whole difference lands in the ``not_recorded`` remainder."""
    from app.services import quota as quota_mod

    async def _boom():
        raise RuntimeError("redis is down")

    monkeypatch.setattr(quota_mod, "redis_client", _boom)
    assert await quota_mod.read_refund_causes("agentic_failure_pool", "2026") == {}


@pytest.mark.asyncio
async def test_non_integer_tally_values_are_skipped(monkeypatch):
    from app.services import quota as quota_mod

    redis = _FakeRedis()
    key = quota_mod._refund_cause_key("agentic_failure_pool", "2026")
    redis.hashes[key] = {"provider_overloaded": 3, "corrupted": "not-a-number"}

    async def _redis():
        return redis

    monkeypatch.setattr(quota_mod, "redis_client", _redis)
    assert await quota_mod.read_refund_causes("agentic_failure_pool", "2026") == {
        "provider_overloaded": 3
    }


# ── The cause reaches the refund API ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_free_refund_forwards_its_cause(monkeypatch):
    from app.services import quota as quota_mod

    seen: dict = {}

    async def _record(
        _redis,
        usage_type,
        window_key,
        cause,
        *,
        user_id,
        counter_key,
        granted_at,
        epoch_token,
    ):
        seen["usage_type"] = usage_type
        seen["cause"] = cause
        seen["user_id"] = user_id
        seen["counter_key"] = counter_key
        seen["granted_at"] = granted_at
        seen["epoch_token"] = epoch_token

    async def _refund(*_a, **_k):
        return None

    class _Window:
        key = "2026"

    async def _resolve_pool_window(_db):
        return _Window()

    monkeypatch.setattr(quota_mod, "_record_refund_cause", _record)
    monkeypatch.setattr(quota_mod, "refund_agentic_usage", _refund)
    # One free credit counted in the window, while the user's epoch is 2.
    monkeypatch.setattr(quota_mod, "_count_free_credit", lambda *_a, **_k: _coro((1, 2)))
    monkeypatch.setattr(
        "app.services.agent_failure_pool.resolve_pool_window", _resolve_pool_window
    )

    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    granted = await quota_mod._apply_free_refund(
        _FakeRedis(),
        user_id,
        agent_id,
        None,
        "monthly",
        "run 1",
        cause=FailureCause.FAILED_BEFORE_MODEL_CALL.value,
    )
    assert granted is True
    assert seen == {
        "usage_type": "agentic_free_refund",
        "cause": "failed_before_model_call",
        "user_id": user_id,
        # The counter that granted the credit, and the value it granted it at.
        "counter_key": quota_mod._free_refund_key(user_id, agent_id, "2026"),
        "granted_at": 1,
        # Filed under the epoch it was counted in.
        "epoch_token": "2",
    }


def test_the_pool_refund_tallies_its_cause_against_its_claim():
    """The per-user tally is what lets a quota reset take a user's causes out
    of the breakdown, and the claim is what lets a tally landing after
    that reset tell it came too late."""
    import inspect

    from app.services import quota as quota_mod

    src = inspect.getsource(quota_mod._claim_failure_pool_slot_and_refund)
    call = src[src.index("await _record_refund_cause(") :]
    call = call[: call.index(")")]
    for argument in ("user_id=user_id", "counter_key=pool_key", "granted_at=claimed"):
        assert argument in call


async def _coro(value):
    return value


# ── Keyspace safety: the tally must not poison the quota flush ───────────────


def test_the_cause_hash_never_breaks_the_quota_flush():
    """The cause tally is a HASH; ``flush_quota_usage`` GETs every key under
    ``_USAGE_PREFIX`` in one pipeline.

    ``GET`` on a hash is ``WRONGTYPE``, and the pipeline raises on the first
    error — so one cause hash inside that keyspace would break the flush for
    EVERY counter and silently stop all quota persistence. This is a naming
    constraint with no runtime guard, which is exactly why it needs a test.
    """
    from app.services.quota import _USAGE_PREFIX, _refund_cause_key

    for usage_type in ("agentic_failure_pool", "agentic_free_refund"):
        for key in (
            _refund_cause_key(usage_type, "2026"),
            _refund_cause_key(usage_type, "2026", _USER),
        ):
            assert not key.startswith(_USAGE_PREFIX), (
                f"{key} sits in the string-only counter keyspace; the quota "
                "flush will GET it and die on WRONGTYPE"
            )


def test_the_cause_hash_is_not_swept_by_the_counter_sweep():
    """Corollary of the above: since the tally is out of that keyspace, the
    recharge's counter sweep cannot reach it — which is why the recharge has to
    clear it explicitly. If this ever starts matching, the explicit clear is
    redundant AND the flush is broken."""
    import fnmatch

    from app.services.quota import (
        _POOL_USAGE_TYPE,
        _USAGE_PREFIX,
        _refund_cause_key,
    )

    sweep = f"{_USAGE_PREFIX}*:{_POOL_USAGE_TYPE}:*"
    assert not fnmatch.fnmatch(_refund_cause_key(_POOL_USAGE_TYPE, "2026"), sweep)
    assert not fnmatch.fnmatch(_refund_cause_key(_POOL_USAGE_TYPE, "2026", _USER), sweep)


@pytest.mark.asyncio
async def test_a_recharge_clears_the_pool_breakdown():
    """After a support-code recharge the pool total is zero, so its breakdown
    must be too — otherwise the card shows a full pre-recharge decomposition
    against a zeroed headline."""
    from app.services.quota import _POOL_USAGE_TYPE, _refund_cause_key, clear_refund_causes

    redis = _FakeRedis()
    redis.hashes[_refund_cause_key(_POOL_USAGE_TYPE, "2026")] = {"provider_overloaded": 4}
    redis.hashes[_refund_cause_key(_POOL_USAGE_TYPE, "2025")] = {"unknown": 1}
    redis.hashes[_refund_cause_key(_POOL_USAGE_TYPE, "2026", _USER)] = {"tool_error": 2}
    free_key = _refund_cause_key("agentic_free_refund", "2026")
    free_user_key = _refund_cause_key("agentic_free_refund", "2026", _USER)
    redis.hashes[free_key] = {"failed_before_model_call": 7}
    redis.hashes[free_user_key] = {"provider_unreachable": 1}

    removed = await clear_refund_causes(redis, _POOL_USAGE_TYPE)

    assert removed == 3, "every window's pool breakdown is cleared, every user's too"
    # Free credits are not rationed and not recharged: their total survives the
    # recharge, so their breakdown must survive with it.
    assert redis.hashes == {
        free_key: {"failed_before_model_call": 7},
        free_user_key: {"provider_unreachable": 1},
    }


@pytest.mark.asyncio
async def test_a_recharge_deletes_the_tallies_in_bounded_batches(monkeypatch):
    """One hash per credited user: a single DEL over all of them would grow
    with the install."""
    from app.services import quota as quota_mod

    redis = _FakeRedis()
    for _ in range(5):
        redis.hashes[
            quota_mod._refund_cause_key(quota_mod._POOL_USAGE_TYPE, "2026", uuid.uuid4())
        ] = {"tool_error": 1}
    monkeypatch.setattr(quota_mod, "_CAUSE_KEY_BATCH", 2)

    assert await quota_mod.clear_refund_causes(redis, quota_mod._POOL_USAGE_TYPE) == 5
    assert [len(keys) for keys in redis.deletes] == [2, 2, 1]
    assert redis.hashes == {}


@pytest.mark.asyncio
async def test_the_tallies_go_while_the_scan_runs(monkeypatch):
    """The recharge and the reset scan the keyspace inside the flush lock and
    meet one hash per credited user: each batch is deleted as the scan reaches
    it, so the keys are never all held at once."""
    from app.services import quota as quota_mod

    redis = _FakeRedis()
    for _ in range(5):
        redis.hashes[
            quota_mod._refund_cause_key(quota_mod._POOL_USAGE_TYPE, "2026", uuid.uuid4())
        ] = {"tool_error": 1}
    events: list[str] = []
    scan, delete = redis.scan_iter, redis.delete

    async def _scan(match="*", count=None):
        async for key in scan(match, count):
            events.append("scanned")
            yield key

    async def _delete(*keys):
        events.append(f"deleted {len(keys)}")
        return await delete(*keys)

    redis.scan_iter, redis.delete = _scan, _delete
    monkeypatch.setattr(quota_mod, "_CAUSE_KEY_BATCH", 2)

    assert await quota_mod.clear_refund_causes(redis, quota_mod._POOL_USAGE_TYPE) == 5
    assert events[:3] == ["scanned", "scanned", "deleted 2"]

    for window in ("2024", "2025", "2026"):
        redis.hashes[
            quota_mod._refund_cause_key(quota_mod._POOL_USAGE_TYPE, window, _USER)
        ] = {"tool_error": 1}
    events.clear()

    assert await quota_mod.forget_user_refund_causes(redis, _USER) == 3
    assert events[:3] == ["scanned", "scanned", "deleted 2"]


@pytest.mark.asyncio
async def test_forgetting_a_user_leaves_everyone_else_alone():
    """A quota reset forgets the user's own tallies (every window, both kinds)
    and the platform-wide tally of the windows it removed credits from - which
    cannot say whose its causes were - and nothing else."""
    from app.services.quota import (
        _FREE_REFUND_USAGE_TYPE,
        _POOL_USAGE_TYPE,
        _refund_cause_key,
        forget_user_refund_causes,
    )

    redis = _FakeRedis()
    kept = {
        _refund_cause_key(_POOL_USAGE_TYPE, "2026", _OTHER): {"tool_error": 1},
        _refund_cause_key(_POOL_USAGE_TYPE, "2025"): {"unknown": 1},
        _refund_cause_key(_FREE_REFUND_USAGE_TYPE, "2026"): {"provider_unreachable": 1},
    }
    gone = {
        _refund_cause_key(_POOL_USAGE_TYPE, "2026", _USER): {"tool_error": 2},
        _refund_cause_key(_POOL_USAGE_TYPE, "2025", _USER): {"tool_error": 1},
        _refund_cause_key(_FREE_REFUND_USAGE_TYPE, "2026", _USER): {"provider_unreachable": 1},
        _refund_cause_key(_POOL_USAGE_TYPE, "2026"): {"unknown": 3},
    }
    redis.hashes.update(kept)
    redis.hashes.update(gone)

    removed = await forget_user_refund_causes(
        redis, _USER, {(_POOL_USAGE_TYPE, "2026")}
    )

    assert redis.hashes == kept
    assert removed == len(gone)


@pytest.mark.asyncio
async def test_forgetting_never_fails_the_reset():
    """The counters are already deleted by this point: the reset must finish."""
    from app.services.quota import _POOL_USAGE_TYPE, forget_user_refund_causes

    assert (
        await forget_user_refund_causes(
            _FakeRedis(fail=True), _USER, {(_POOL_USAGE_TYPE, "2026")}
        )
        == 0
    )


@pytest.mark.asyncio
async def test_clearing_a_broken_breakdown_never_fails_the_recharge():
    """The counters are already zeroed by this point. A bookkeeping error must
    not fail a redemption the admin has already spent a code on."""
    from app.services.quota import _POOL_USAGE_TYPE, clear_refund_causes

    assert await clear_refund_causes(_FakeRedis(fail=True), _POOL_USAGE_TYPE) == 0


def test_the_cause_hash_outlives_its_window():
    """The pool window is a full licence year; a shorter TTL would decay the
    breakdown month by month while the totals persist, growing ``not_recorded``
    with no signal that it happened."""
    from app.services.quota import _POOL_KEY_TTL

    assert _POOL_KEY_TTL >= 366 * 86_400


# ── The billing-candidate set stays inside the taxonomy ─────────────────────


def test_billable_candidates_are_all_real_causes():
    """A stale member would silently never match, quietly shrinking what phase
    2 is allowed to consider."""
    from app.services.failure_causes import BILLABLE_CANDIDATE_CAUSES

    assert set(FailureCause) >= BILLABLE_CANDIDATE_CAUSES
