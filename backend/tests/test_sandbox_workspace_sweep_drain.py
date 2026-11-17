"""The sandbox workspace sweep drains its backlog.

Three causes kept the ended-run backlog in place: objects deleted one
request per key, an hour-long lease that a sweep cut by a rollout kept, and
the freed counters written only when a sweep finished. Each is held here
without a store or a Redis server: a fake Minio client and a fake Redis.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from minio.error import S3Error

import app.services.file_storage as fs
from app.services.sandbox_workspace import gc


class _Redis:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.ttls: dict[str, int | None] = {}
        self.hashes: dict[str, dict[str, int]] = {}

    async def exists(self, key: str) -> int:
        return int(key in self.values)

    async def set(self, key: str, value: object, *, nx: bool = False, ex: int | None = None):
        if nx and key in self.values:
            return None
        self.values[key] = value
        self.ttls[key] = ex
        return True

    async def delete(self, key: str) -> int:
        self.ttls.pop(key, None)
        return int(self.values.pop(key, None) is not None)

    async def hincrby(self, key: str, field: str, amount: int) -> int:
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = bucket.get(field, 0) + amount
        return bucket[field]


@pytest.fixture
def redis(monkeypatch):
    fake = _Redis()

    async def _client():
        return fake

    import app.core.redis as redis_module

    monkeypatch.setattr(redis_module, "redis_client", _client)
    return fake


def _s3_error(code: str) -> S3Error:
    return S3Error(code, code, "resource", "request", "host", None)


class _DeleteObject:
    def __init__(self, name: str) -> None:
        self.key = name


def _storage(monkeypatch, remove_objects) -> tuple[fs.FileStorageService, list[str]]:
    import minio.deleteobjects

    monkeypatch.setattr(minio.deleteobjects, "DeleteObject", _DeleteObject)
    service = fs.FileStorageService()
    singles: list[str] = []
    service._client = SimpleNamespace(
        remove_objects=remove_objects,
        remove_object=lambda _bucket, name: singles.append(name),
    )
    return service, singles


async def test_delete_many_sends_one_request_per_thousand_keys(monkeypatch):
    requests: list[list[str]] = []

    def remove_objects(_bucket, objects):
        requests.append([obj.key for obj in objects])
        return iter(())

    service, singles = _storage(monkeypatch, remove_objects)
    keys = [f"sandbox-workspaces/w/{i}" for i in range(2500)]
    assert await service.delete_many(keys + keys[:10] + [""]) == 2500
    assert [len(batch) for batch in requests] == [1000, 1000, 500]
    assert singles == []


async def test_delete_many_counts_a_missing_key_as_removed_and_a_refused_one_not(monkeypatch):
    def remove_objects(_bucket, objects):
        names = [obj.key for obj in objects]
        return iter(
            [
                SimpleNamespace(name=names[0], code="NoSuchKey", message="gone"),
                SimpleNamespace(name=names[1], code="AccessDenied", message="denied"),
            ]
        )

    service, _ = _storage(monkeypatch, remove_objects)
    assert await service.delete_many(["a", "b", "c"]) == 2


async def test_delete_many_stops_on_an_unreachable_store_and_falls_back_without_the_bulk_api(monkeypatch):
    def unreachable(_bucket, objects):
        raise ConnectionError("down")

    service, _ = _storage(monkeypatch, unreachable)
    assert await service.delete_many([f"k{i}" for i in range(1500)]) == 0

    def not_implemented(_bucket, objects):
        raise _s3_error("NotImplemented")

    service, singles = _storage(monkeypatch, not_implemented)
    assert await service.delete_many(["a", "b"]) == 2
    assert singles == ["a", "b"]


async def test_a_sweep_cut_short_is_resumed_once_its_short_lease_expires(redis):
    assert await gc.take_lease() is True
    assert redis.ttls[gc._LEASE_KEY] == gc._LEASE_SECONDS
    assert gc._LEASE_SECONDS <= 600 < gc.SWEEP_INTERVAL_SECONDS
    # Held while the sweep runs.
    assert await gc.take_lease() is False
    # A rollout kills the sweep: nothing is recorded and the lease expires.
    await redis.delete(gc._LEASE_KEY)
    assert await gc.take_lease() is True


async def test_a_finished_sweep_spaces_the_next_one_an_interval_later(redis):
    from datetime import UTC, datetime

    assert await gc.take_lease() is True
    report = gc.SweepReport()
    report.add("run_ended", 10)
    await gc._record_sweep(report, finished_at=datetime.now(UTC))
    assert gc._LEASE_KEY not in redis.values
    assert redis.ttls[gc._SWEPT_KEY] == gc.SWEEP_INTERVAL_SECONDS - 60
    assert await gc.take_lease() is False
    assert '"run_ended": 1' in str(redis.values[gc._LAST_SWEEP_KEY])


async def test_what_a_collect_frees_is_counted_without_touching_the_last_sweep(redis):
    report = gc.SweepReport()
    report.add("run_ended", 100)
    report.add("run_ended", 50)
    await gc._record_freed(report)
    assert redis.hashes[gc._FREED_KEY] == {"run_ended:workspaces": 2, "run_ended:bytes": 150}
    assert gc._LAST_SWEEP_KEY not in redis.values
    totals = gc.SweepReport()
    totals.merge(report)
    totals.merge(report)
    assert totals.deleted == {"run_ended": 4} and totals.bytes_freed == {"run_ended": 300}
