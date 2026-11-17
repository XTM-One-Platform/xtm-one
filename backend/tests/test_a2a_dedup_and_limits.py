"""Deduplication and rate limiting — the only controls on external load.

The specification withdraws any latency target and any cap on concurrent
external runs, naming per-registration rate limiting as the sole control. That
makes the fail-closed behaviour below load-bearing rather than defensive: if
the limiter silently skips when Redis is down, the feature has no control at
all at exactly the moment the platform is degraded.
"""

import pytest

from app.a2a import tasks as task_svc
from app.api.v1 import a2a_server
from app.core.config import settings
from tests.a2a_server_stubs import own_dedup_claims


class TestDeduplicationClaim:
    async def test_first_claim_succeeds(self, monkeypatch):
        redis = own_dedup_claims(monkeypatch)
        assert await task_svc._claim_message_id("c1", "m1") is True
        # A failing client answers True too; only the key proves the claim.
        assert redis.keys == {task_svc._dedup_key("c1", "m1"): "1"}

    async def test_repeat_claim_is_refused(self, monkeypatch):
        own_dedup_claims(monkeypatch)
        await task_svc._claim_message_id("c1", "m1")
        assert await task_svc._claim_message_id("c1", "m1") is False

    async def test_claims_are_scoped_per_registration(self, monkeypatch):
        """One partner's message ids must not collide with another's."""
        own_dedup_claims(monkeypatch)
        await task_svc._claim_message_id("c1", "m1")
        assert await task_svc._claim_message_id("c2", "m1") is True

    async def test_redis_failure_defers_to_the_unique_constraint(self, monkeypatch):
        """Redis is the fast path, not the correctness boundary. Failing
        closed here would refuse legitimate first-time submissions during a
        blip; the (client_id, message_id) constraint is the real guarantee."""

        calls: list[int] = []

        async def _boom():
            calls.append(1)
            raise RuntimeError("redis down")

        monkeypatch.setattr("app.core.redis.redis_client", _boom)
        assert await task_svc._claim_message_id("c1", "m1") is True
        # Without Redis in the environment an unreached patch also answers
        # True, from the real client failing; only the call proves the path.
        assert calls, "the simulated outage was never reached"

    def test_dedup_key_includes_both_parts(self):
        key = task_svc._dedup_key("client", "msg")
        assert "client" in key and "msg" in key

    def test_window_is_configured_not_hardcoded(self):
        assert settings.a2a_dedup_window_seconds == 86400


async def _coro(value):
    return value


class TestRateLimitFailsClosed:
    @staticmethod
    def _outage():
        """A Redis client factory that fails, and records that it was asked.

        The record is the point. Wherever Redis is unreachable - the
        CI unit job runs without it - a patch that misses the limiter still
        ends in a 503, from the real client's refused connection, so the
        status code alone cannot tell "the outage was simulated" from "the
        patch reached nothing". Only the fake having been awaited can."""
        calls: list[int] = []

        async def _boom():
            calls.append(1)
            raise RuntimeError("redis down")

        return _boom, calls

    async def test_redis_outage_refuses_rather_than_admits(self, monkeypatch):
        from fastapi import HTTPException

        _boom, calls = self._outage()

        # Patch the name the limiter actually reads. ``rate_limit`` binds
        # ``redis_client`` with a module-level ``from`` import, as every other
        # Redis caller in the codebase does, so rebinding it on
        # ``app.core.redis`` reaches nothing once that module is imported —
        # and whether it is imported yet depends on what ran earlier in the
        # session. Patching there passed alone and failed in a full run;
        # the four refund tests below already patch it here.
        # No ``raising=False``: a rename should fail loudly, not no-op.
        monkeypatch.setattr("app.core.rate_limit.redis_client", _boom)
        with pytest.raises(HTTPException) as exc:
            await a2a_server._enforce_a2a_rate_limit("c1", "send")
        assert exc.value.status_code == 503
        assert calls, "the 503 did not come from the simulated outage"

    async def test_redis_outage_refuses_when_the_limiter_is_already_imported(
        self, monkeypatch
    ):
        """The ordering that used to pass for the wrong reason: once
        ``app.core.rate_limit`` is imported it holds its own copy of
        ``redis_client``, and only a patch aimed at that copy reaches it."""
        from fastapi import HTTPException

        import app.core.rate_limit  # noqa: F401 - the point of the test

        _boom, calls = self._outage()
        monkeypatch.setattr("app.core.rate_limit.redis_client", _boom)
        with pytest.raises(HTTPException) as exc:
            await a2a_server._enforce_a2a_rate_limit("c1", "read")
        assert exc.value.status_code == 503
        assert calls, "the patch missed the limiter's own copy of redis_client"

    def test_platform_default_remains_open(self):
        """The override is local to this surface; internal behaviour and the
        platform-wide default are deliberately untouched."""
        assert settings.rate_limit_fail_mode == "open"

    def test_creation_is_limited_more_tightly_than_reads(self):
        # One submission starts a full agent run; one read is a single
        # indexed lookup. A single number would either throttle the polling
        # the architecture requires or admit a run-queue flood.
        assert (
            settings.a2a_task_create_rate_limit < settings.a2a_task_read_rate_limit
        )
        assert settings.a2a_task_create_rate_limit == 10
        # 1800, not research R8's 120: Foundry polls ``tasks/get`` in a
        # zero-backoff loop measured at 8-10/s, and surfaces any 429 as a
        # failed delegation rather than honouring ``Retry-After``. 600 sits
        # at the sustained rate and clips on bursts; 30/s gives 3x margin.
        assert settings.a2a_task_read_rate_limit == 1800


class TestPreauthRefund:
    """The pre-auth per-IP budget must charge only requests that never
    authenticate. It runs before the token is looked at, so it charges
    everyone up front and the handler refunds on successful auth. Without
    the refund its 60/min cap binds every legitimate call, and no raise of
    the per-registration limits helps, because the smaller bucket fires
    first — Foundry polls ``tasks/get`` at ~10/s.
    """

    class _CountingRedis:
        def __init__(self, store=None):
            self.store = store if store is not None else {}

        async def eval(self, script, numkeys, key, *args):
            if "INCR" in script:
                self.store[key] = self.store.get(key, 0) + 1
                return self.store[key]
            # the refund script: decrement only an existing positive counter
            current = self.store.get(key)
            if current and current > 0:
                self.store[key] = current - 1
                return self.store[key]
            return 0

        async def ttl(self, key):
            return 30

    async def test_refund_returns_the_charge(self, monkeypatch):
        from app.core.rate_limit import enforce_rate_limit, refund_rate_limit

        redis = self._CountingRedis()
        monkeypatch.setattr(
            "app.core.rate_limit.redis_client", lambda: _coro(redis), raising=False
        )
        await enforce_rate_limit(
            None, "a2a:preauth", limit=60, window_seconds=60, bucket="ip1"
        )
        await refund_rate_limit(None, "a2a:preauth", bucket="ip1")
        assert redis.store["rl:a2a:preauth:ip1"] == 0

    async def test_refund_never_creates_or_underflows_a_bucket(self, monkeypatch):
        from app.core.rate_limit import refund_rate_limit

        redis = self._CountingRedis()
        monkeypatch.setattr(
            "app.core.rate_limit.redis_client", lambda: _coro(redis), raising=False
        )
        await refund_rate_limit(None, "a2a:preauth", bucket="ip1")
        assert "rl:a2a:preauth:ip1" not in redis.store
        redis.store["rl:a2a:preauth:ip1"] = 0
        await refund_rate_limit(None, "a2a:preauth", bucket="ip1")
        assert redis.store["rl:a2a:preauth:ip1"] == 0

    async def test_refund_swallows_redis_failure(self, monkeypatch):
        """Best-effort: a lost refund under-counts one window's budget, which
        must never fail the authenticated request it rides on."""
        from app.core.rate_limit import refund_rate_limit

        class _Broken:
            async def eval(self, *a):
                raise ConnectionError("down")

        monkeypatch.setattr(
            "app.core.rate_limit.redis_client", lambda: _coro(_Broken()), raising=False
        )
        await refund_rate_limit(None, "a2a:preauth", bucket="ip1")

    async def test_authenticated_traffic_leaves_the_preauth_budget_intact(
        self, monkeypatch
    ):
        """The pair end to end: N charges + N refunds = untouched budget."""
        from app.core.rate_limit import enforce_rate_limit, refund_rate_limit

        redis = self._CountingRedis()
        monkeypatch.setattr(
            "app.core.rate_limit.redis_client", lambda: _coro(redis), raising=False
        )
        for _ in range(200):  # well past the 60/min cap
            await enforce_rate_limit(
                None, "a2a:preauth", limit=60, window_seconds=60, bucket="ip1"
            )
            await refund_rate_limit(None, "a2a:preauth", bucket="ip1")
        assert redis.store["rl:a2a:preauth:ip1"] == 0


class TestPlatformKeysAreRefused:
    @pytest.mark.parametrize("prefix", ["fcp-", "mcp-"])
    def test_unscoped_platform_keys_are_rejected_by_prefix(self, prefix):
        """These are unscoped and non-expiring; accepting one here would hand
        a partner a bearer of its owner's full platform identity."""
        assert prefix in a2a_server._REJECTED_KEY_PREFIXES

    @pytest.mark.parametrize("prefix", ["fcp-", "mcp-"])
    async def test_rejection_is_distinguishable_internally(self, prefix):
        """FR-020's audit needs to know a PLATFORM key was refused — that is
        the migration signal an operator watches for. The reason label is
        internal only: the caller's 401 is identical to every other refusal."""

        class _Request:
            headers = {"authorization": f"Bearer {prefix}abc"}

        class _Exposure:
            id = None

        client_id, reason, _provider = await a2a_server._authenticate(
            _Request(), db=None, exposure=_Exposure()
        )
        assert client_id is None
        assert reason == "rejected_platform_key"

    async def test_a_missing_token_is_a_plain_unauthenticated(self):
        class _Request:
            headers = {}

        client_id, reason, _provider = await a2a_server._authenticate(
            _Request(), db=None, exposure=None
        )
        assert client_id is None
        assert reason == "unauthenticated"


@pytest.mark.integration
class TestAgainstRealInfrastructure:
    # NOT YET WRITTEN. The body was an unconditional ``pytest.skip``, which the
    # ``integration`` marker could never select past — so this read as coverage
    # of the IntegrityError dedup recovery while asserting nothing, in any run.
    # Declared unimplemented instead, so the gap is visible in test output:
    # concurrent inserts on the real partial unique index are the only thing
    # that exercises that recovery, and the stubs elsewhere fabricate rowcounts.
    @pytest.mark.skip(reason="unimplemented: needs the real Postgres + Redis harness")
    async def test_concurrent_retries_start_exactly_one_run(self):
        raise AssertionError("unimplemented")
