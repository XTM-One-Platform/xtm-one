"""What charging an agentic action writes to Redis.

The live counter of the web chat reads the period key ``increment_agentic_usage``
returns; the charge itself stays what it was - the same counters and keys, the
same increments and TTLs, in the same order - whether a caller reads the key or
not. Pinned with a recording Redis on a frozen clock, for a chat turn and for a
background task that leaves a metering receipt. Nothing here depends on the live
counter, so the file runs unchanged against the code before it.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app.services import agent_chat, agentic_executions, quota
from tests.quota_redis_scripts import run_quota_script

USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
AGENT = uuid.UUID("22222222-2222-2222-2222-222222222222")


class _RecordingRedis:
    """Every command, in order; counters count, nothing is stored before."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._counters: dict[str, int] = {}

    async def eval(self, script, numkeys, *keys_and_args):
        name = {
            quota._COUNT_GLOBAL_LUA: "count_global",
            quota._WRITE_RECEIPT_LUA: "write_receipt",
        }[script]
        self.calls.append((name, keys_and_args[:numkeys], keys_and_args[numkeys:]))
        return run_quota_script(self._counters, {}, script, keys_and_args)

    async def incr(self, key):
        self.calls.append(("incr", key))
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    async def expire(self, key, ttl):
        self.calls.append(("expire", key, ttl))
        return True

    async def getdel(self, key):
        self.calls.append(("getdel", key))
        return None

    async def set(self, key, value, ex=None):
        self.calls.append(("set", key, value, ex))
        return True


class _FrozenClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 27, 21, 30, tzinfo=tz)


@pytest.fixture
def redis(monkeypatch):
    fake = _RecordingRedis()
    monkeypatch.setattr(quota, "redis_client", AsyncMock(return_value=fake))
    monkeypatch.setattr(agentic_executions, "redis_client", AsyncMock(return_value=fake))
    monkeypatch.setattr(quota, "datetime", _FrozenClock)
    monkeypatch.setattr(agentic_executions, "datetime", _FrozenClock)
    # The check only reads; the charge is what writes.
    monkeypatch.setattr(quota, "check_agentic_quota", AsyncMock(return_value=None))
    return fake


def _charge_writes(*tail: tuple) -> list[tuple]:
    user = f"quota:usage:{USER}:agentic:_total:2026-09"
    view = f"quota:usage:{USER}:agentic_view:_total"
    totals = tuple(f"quota:usage:_global:agentic:_total:{key}" for key in ("2026", "2026-09", "2026-09-27"))
    return [
        ("incr", "agentic:exec:2026-09-27:agent_chat"),
        ("expire", "agentic:exec:2026-09-27:agent_chat", 86_400 * 40),
        # The platform-wide counters first, every panel period in one script:
        # Redis does not hold them, so they are seeded from their saved count
        # (none here) as they count.
        ("count_global", totals, (86_400 * 35, "", "", "")),
        ("count_global", totals, (86_400 * 35, 0, 0, 0)),
        ("getdel", user),
        ("incr", f"{user}:_all"),
        ("expire", f"{user}:_all", 86_400 * 35),
        ("incr", f"{user}:{AGENT}"),
        ("expire", f"{user}:{AGENT}", 86_400 * 35),
        # The quota panel's counters: today, this month, this year.
        ("incr", f"{view}:2026-09-27:_all"),
        ("expire", f"{view}:2026-09-27:_all", 86_400 * 2),
        ("incr", f"{view}:2026-09:_all"),
        ("expire", f"{view}:2026-09:_all", 86_400 * 35),
        ("incr", f"{view}:2026:_all"),
        ("expire", f"{view}:2026:_all", 86_400 * 400),
        *tail,
    ]


async def test_a_charged_chat_turn_writes_what_it_always_wrote(redis):
    out: dict = {}
    refusal = await agent_chat.enforce_agentic_quota_ex(
        None, str(USER), agent_id=str(AGENT), quota_out=out
    )
    assert refusal == (None, True)
    assert out["actions_consumed"] == 1 and out["reason"] == "charged"
    assert redis.calls == _charge_writes()


async def test_a_charged_background_task_writes_its_receipt_as_before(redis):
    token = "sub_agent_task:33333333-3333-3333-3333-333333333333:1"
    await agent_chat.enforce_agentic_quota_ex(
        None, str(USER), agent_id=str(AGENT), receipt_token=token
    )
    receipt = json.dumps({"user_id": str(USER), "agent_id": str(AGENT), "period": "monthly"})
    # Written unless its attempt was already replaced.
    assert redis.calls == _charge_writes(
        ("write_receipt", (f"quota:receipt:{token}",), (receipt, 86_400 * 7, "redeemed"))
    )
