"""Which workers are alive, and how the last one went. An in-memory Redis.

* a worker says it is there; one whose word ran out while its last word is
  kept is gone, one that said it stopped is gone, one that never published is
  never taken for gone, and an unreachable Redis takes nobody for gone;
* on this very host, a worker whose process is no longer the one that wrote
  its word (a container restarted in place) is gone at once, its word still
  fresh;
* how a worker went is said: the stop asked for (its signal), the stop the
  orchestrator cut short, or the signal and the memory of its last word;
* the next worker says whether a stop was a deploy or a restart;
* a worker killed mid-render is found gone within 30 s of the kill.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.core import redis as redis_module
from app.core.config import settings
from app.services import worker_presence


class _Redis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.down = False

    def _check(self) -> None:
        if self.down:
            raise ConnectionError("Redis is down")

    async def set(self, key, value, ex=None):
        self._check()
        self.values[key], self.ttl[key] = value, ex

    async def delete(self, key):
        self._check()
        self.values.pop(key, None)

    async def mget(self, keys):
        self._check()
        return [self.values.get(key) for key in keys]

    def lapse(self, worker_id: str) -> None:
        """Its presence ran out: only its last word is left."""
        self.values.pop(f"xtm:orchestration:worker:{worker_id}", None)


@pytest.fixture
def redis(monkeypatch):
    fake = _Redis()

    async def client():
        return fake

    monkeypatch.setattr(redis_module, "redis_client", client)
    monkeypatch.setattr(worker_presence, "_published", {})
    return fake


def _elsewhere(record: dict) -> dict:
    """*record* as written by a worker of another host."""
    return {**record, "host": "worker-7f9c", "boot_id": "another-boot"}


async def test_a_worker_whose_word_ran_out_is_gone_one_that_never_spoke_is_not(redis):
    await worker_presence.publish("w-alive")
    await worker_presence.publish("w-killed")
    for worker_id in ("w-alive", "w-killed"):
        key = f"xtm:orchestration:worker:{worker_id}"
        redis.values[key] = json.dumps(_elsewhere(json.loads(redis.values[key])))
        redis.values[f"xtm:orchestration:worker-last:{worker_id}"] = redis.values[key]
    assert redis.ttl["xtm:orchestration:worker:w-alive"] == worker_presence.PRESENCE_TTL_SECONDS
    redis.lapse("w-killed")
    gone = await worker_presence.gone(["w-alive", "w-killed", "w-old-release", "me"], me="me")
    assert set(gone) == {"w-killed"} and gone["w-killed"]["lapsed"] is True
    # Redis unreachable: nobody is taken for gone.
    redis.down = True
    assert await worker_presence.gone(["w-killed"], me="me") == {}


async def test_a_worker_that_said_it_stopped_is_gone(redis):
    await worker_presence.publish("w-stopped", state=worker_presence.STATE_STOPPED, signal="SIGTERM")
    assert "xtm:orchestration:worker:w-stopped" not in redis.values
    gone = await worker_presence.gone(["w-stopped"], me="me")
    assert gone["w-stopped"]["state"] == "stopped" and gone["w-stopped"]["signal"] == "SIGTERM"
    # It stopped its executions that stop on a lost lease before saying so: an evaluation it ran is taken back at
    # once (``orchestration_reconciler.waits_for_lease_of``).
    assert gone["w-stopped"][worker_presence.STOPS_LEASE_LOSS_WORK] is True


async def test_a_container_restarted_in_place_finds_its_predecessor_gone_at_once(redis, monkeypatch):
    """Same host, same boot, another process: the word is fresh, the process that wrote it is not there."""
    before = {"worker_id": "w-before", "host": "worker-0", "boot_id": "boot-1", "pid": 1, "started": 100, "state": "running"}
    redis.values["xtm:orchestration:worker:w-before"] = json.dumps(before)
    # The new worker is PID 1 again, started later: the PID is taken, never by the process that wrote the word.
    monkeypatch.setattr(
        worker_presence, "identity", lambda: {"host": "worker-0", "boot_id": "boot-1", "pid": 1, "started": 9000}
    )
    alive = {"answer": False}
    monkeypatch.setattr(worker_presence, "_process_alive", lambda _pid, _started: alive["answer"])
    assert "w-before" in await worker_presence.gone(["w-before"], me="w-after")
    # Two workers of one host, both alive: neither is taken for gone.
    alive["answer"] = True
    assert await worker_presence.gone(["w-before"], me="w-after") == {}


def test_how_a_worker_went_is_said_with_its_signal_and_its_memory():
    at = (datetime.now(UTC) - timedelta(seconds=21)).isoformat()
    record = {
        "host": "worker-7f9c",
        "version": "1.4.2",
        "state": "running",
        "at": at,
        "memory": int(3.9 * (1 << 30)),
        "memory_limit": 4 << 30,
        "lapsed": True,
    }
    said = worker_presence.stop_reason(record)
    assert said.startswith("its worker on worker-7f9c (version 1.4.2) went without a stop request, last heard 2")
    assert "using 3.9 GiB of its 4.0 GiB limit (97%) then" in said
    assert "most likely killed by the kernel's out-of-memory killer (SIGKILL" in said
    calm = worker_presence.stop_reason({**record, "memory": 1 << 30, "version": settings.app_version})
    assert "killed by a signal no process can catch (SIGKILL) or crashed" in calm
    # An older release gone silent under its memory limit: the rollout that replaced it killed it.
    rolled = worker_presence.stop_reason({**record, "memory": 1 << 30, "version": f"{settings.app_version}-old"})
    assert "most likely killed by the rollout that replaced its release" in rolled
    stopped = worker_presence.stop_reason({**record, "state": "stopped", "signal": "SIGTERM"})
    assert stopped == "its worker on worker-7f9c (version 1.4.2) was stopped (SIGTERM) and did not hand it back"
    cut = worker_presence.stop_reason({**record, "state": "draining", "signal": "SIGTERM"})
    assert "was asked to stop (SIGTERM) and was killed before it handed it back" in cut


def test_the_next_worker_tells_a_deploy_from_a_restart(monkeypatch):
    monkeypatch.setattr(settings, "app_version", "1.4.2")
    assert worker_presence.resumed_after("1.4.1") == "after a deploy (1.4.1 -> 1.4.2)"
    assert worker_presence.resumed_after("1.4.2") == "after its worker restarted (version 1.4.2)"
    assert worker_presence.resumed_after(None) == "after its worker stopped"


def test_a_worker_killed_mid_render_is_found_gone_within_30_seconds_of_the_kill():
    """The kill lands just after a word: the word lasts its TTL, then the next cycle of any worker finds it gone."""
    worst = worker_presence.PRESENCE_TTL_SECONDS + settings.reconciler_interval_seconds
    assert worst < 30 and worker_presence.PUBLISH_EVERY_SECONDS < worker_presence.PRESENCE_TTL_SECONDS / 2
