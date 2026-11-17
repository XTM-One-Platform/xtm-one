"""Operator notices said once for the whole platform per interval.

A notice latched per process was said again by every uvicorn worker, after
every ``MAX_REQUESTS`` recycle and on every replica: the custom tool sandbox
notice 4 times in 12 s on one pod, the undeclared proxy notice 5 times in 30
minutes. :mod:`app.core.log_once` claims the notice in Redis for its interval;
the process that wins says it with the count of what every process held back
since, the others stay silent, and a Redis that cannot answer leaves each
process to say it itself.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.core import log_once
from app.core.config import settings
from tests.notice_store import FakeNoticeRedis, new_process, share_notices

NOTICE = logging.getLogger("tests.log_once.notice")
KEY = "custom_tool_subprocess_sandbox"
DAY = 24 * 60 * 60.0
TEXT = "Custom tools run in a plain subprocess."


@pytest.fixture
def clock(monkeypatch) -> SimpleNamespace:
    """The notices' own clock, moved by hand; the event loop keeps the real one."""
    now = SimpleNamespace(value=1000.0)
    monkeypatch.setattr(log_once, "time", SimpleNamespace(monotonic=lambda: now.value))
    return now


@pytest.fixture(autouse=True)
def _capture(caplog):
    with caplog.at_level(logging.DEBUG, logger=NOTICE.name):
        yield


def _said(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == NOTICE.name]


async def _say(message: str = TEXT, *args: object, key: str = KEY) -> bool:
    return await log_once.log_once(NOTICE, logging.WARNING, key, message, *args, interval=DAY)


async def test_the_processes_of_the_platform_say_it_once_between_them(monkeypatch, caplog):
    share_notices(monkeypatch)
    said = []
    for _ in range(5):
        new_process()
        said.append(await _say())
    assert said == [True, False, False, False, False]
    [record] = _said(caplog)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == TEXT


async def test_the_next_line_counts_what_the_other_processes_held_back(monkeypatch, caplog):
    redis = share_notices(monkeypatch)
    await _say()
    for _ in range(2):
        new_process()
        assert not await _say()
    redis.interval_elapsed()
    new_process()
    assert await _say()
    first, second = (r.getMessage() for r in _said(caplog))
    assert first == TEXT
    assert second == f"{TEXT} (2 more since the last one)"


async def test_the_next_line_counts_what_this_process_held_back(monkeypatch, caplog, clock):
    redis = share_notices(monkeypatch)
    await _say()
    for _ in range(3):
        assert not await _say()
    redis.interval_elapsed()
    clock.value += DAY
    assert await _say()
    assert _said(caplog)[-1].getMessage() == f"{TEXT} (3 more since the last one)"


async def test_within_its_window_a_process_asks_redis_once(monkeypatch):
    redis = share_notices(monkeypatch)
    for _ in range(50):
        await _say()
    assert redis.commands == 2, "one SET NX and one GETDEL, then nothing until the window ends"


async def test_the_claim_lasts_the_interval_and_carries_the_release(monkeypatch):
    redis = share_notices(monkeypatch)
    monkeypatch.setattr(settings, "app_version", "1.0.0")
    assert await _say()
    new_process()
    monkeypatch.setattr(settings, "app_version", "1.1.0")
    assert await _say(), "a new release says it again"
    assert sorted(redis.claims()) == [f"log_once:1.0.0:{KEY}", f"log_once:1.1.0:{KEY}"]
    assert redis.ttls[f"log_once:1.1.0:{KEY}"] == 86400


async def test_the_arguments_are_formatted_by_the_logger(monkeypatch, caplog):
    share_notices(monkeypatch)
    await _say("Provider webhook %s triggered %d assignment(s)", "github", 3)
    assert _said(caplog)[0].getMessage() == "Provider webhook github triggered 3 assignment(s)"


@pytest.mark.parametrize(
    "broken",
    [lambda: FakeNoticeRedis(fail=True), lambda: FakeNoticeRedis(hang=True)],
    ids=["refused", "never-answers"],
)
async def test_when_redis_cannot_answer_each_process_says_it_itself(monkeypatch, caplog, broken):
    monkeypatch.setattr(log_once, "_REDIS_TIMEOUT_SECONDS", 0.05)
    share_notices(monkeypatch, broken())
    for _ in range(2):
        new_process()
        assert await _say()
    assert [r.getMessage() for r in _said(caplog)] == [TEXT, TEXT]


async def test_a_claim_redis_decided_holds_when_the_count_fails(monkeypatch, caplog):
    class _CountFails(FakeNoticeRedis):
        async def incrby(self, key: str, amount: int) -> int:
            raise ConnectionError("lost mid-way")

    share_notices(monkeypatch, _CountFails())
    assert await _say()
    new_process()
    assert not await _say(), "another process holds the claim: it stays silent"
    assert [r.getMessage() for r in _said(caplog)] == [TEXT]


class _CountWaits(FakeNoticeRedis):
    """The claim is decided, and the count step waits for ``go``."""

    def __init__(self) -> None:
        super().__init__()
        self.counting = asyncio.Event()
        self.go = asyncio.Event()

    async def getdel(self, key: str) -> str | None:
        self.counting.set()
        await self.go.wait()
        return await super().getdel(key)

    async def incrby(self, key: str, amount: int) -> int:
        self.counting.set()
        await self.go.wait()
        return await super().incrby(key, amount)


async def test_a_caller_cancelled_after_the_claim_was_won_still_says_it(monkeypatch, caplog):
    """The claim held, nobody left to say it: the notice was silent for the whole interval."""
    redis = share_notices(monkeypatch, _CountWaits())
    caller = asyncio.create_task(_say())
    await redis.counting.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    redis.go.set()
    await log_once.settle()
    [record] = _said(caplog)
    assert record.getMessage() == TEXT
    new_process()
    assert not await _say(), "said by the process that holds the claim"


async def test_a_decision_the_event_loop_cancels_releases_its_claim(monkeypatch, caplog):
    redis = share_notices(monkeypatch, _CountWaits())
    log_once.log_once_soon(NOTICE, logging.WARNING, KEY, TEXT, interval=DAY)
    await redis.counting.wait()
    [decision] = log_once._tasks
    decision.cancel()
    await log_once.settle()
    assert redis.claims() == []
    redis.go.set()
    new_process()
    assert await _say()
    assert [r.getMessage() for r in _said(caplog)] == [TEXT]


async def test_a_cancelled_decision_never_releases_the_claim_of_another_process(monkeypatch, caplog):
    redis = share_notices(monkeypatch, _CountWaits())
    claim = f"log_once:{settings.app_version}:{KEY}"
    redis.values[claim] = "another process"
    log_once.log_once_soon(NOTICE, logging.WARNING, KEY, TEXT, interval=DAY)
    await redis.counting.wait()
    [decision] = log_once._tasks
    decision.cancel()
    await log_once.settle()
    assert redis.values[claim] == "another process"
    assert _said(caplog) == []


async def test_a_notice_keyed_by_its_configuration_is_said_again_when_it_changes(monkeypatch, caplog):
    share_notices(monkeypatch)
    for forwarded_allow_ips in ("127.0.0.1", "127.0.0.1", "10.0.0.0/8"):
        new_process()
        await _say(key=f"undeclared_proxy:x-forwarded-for:{log_once.fingerprint(forwarded_allow_ips)}")
    assert len(_said(caplog)) == 2


def test_a_fingerprint_is_short_and_reads_the_configuration_whole():
    assert log_once.fingerprint("auto", True) == log_once.fingerprint("auto", True)
    assert log_once.fingerprint("auto", True) != log_once.fingerprint("auto", False)
    assert log_once.fingerprint(["a", "b"]) != log_once.fingerprint(["a"], "b")
    assert len(log_once.fingerprint("x" * 10_000)) == 12


def test_outside_an_event_loop_the_process_decides_alone(monkeypatch, caplog):
    redis = share_notices(monkeypatch)
    log_once.log_once_soon(NOTICE, logging.WARNING, KEY, TEXT, interval=DAY)
    log_once.log_once_soon(NOTICE, logging.WARNING, KEY, TEXT, interval=DAY)
    assert [r.getMessage() for r in _said(caplog)] == [TEXT]
    assert redis.commands == 0


async def test_a_synchronous_caller_leaves_the_claim_to_a_task(monkeypatch, caplog):
    share_notices(monkeypatch)
    log_once.log_once_soon(NOTICE, logging.WARNING, KEY, TEXT, interval=DAY)
    assert _said(caplog) == [], "the caller never waits for Redis"
    await log_once.settle()
    new_process()
    log_once.log_once_soon(NOTICE, logging.WARNING, KEY, TEXT, interval=DAY)
    await log_once.settle()
    assert [r.getMessage() for r in _said(caplog)] == [TEXT]


async def test_without_a_shared_store_a_process_says_it_once_per_interval(caplog, clock):
    assert log_once.shared_store is None, "the unit suite decides per process (conftest)"
    assert await _say()
    assert not await _say()
    clock.value += DAY
    assert await _say()
    assert [r.getMessage() for r in _said(caplog)] == [TEXT, f"{TEXT} (1 more since the last one)"]


async def test_each_key_is_its_own_notice(monkeypatch, caplog):
    share_notices(monkeypatch)
    assert await _say(key="undeclared_proxy:x-forwarded-for")
    assert await _say(key="undeclared_proxy:x-real-ip")
    assert len(_said(caplog)) == 2


async def test_the_windows_a_process_keeps_are_bounded(monkeypatch):
    monkeypatch.setattr(log_once, "_MAX_TRACKED_KEYS", 3)
    for index in range(10):
        await _say(key=f"webhook_selection:{index}:legacy")
    assert len(log_once._windows) == 3
