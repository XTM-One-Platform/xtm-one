"""The startup gate reaches object storage through the S3 API, so AWS S3 passes it.

It used to GET ``/minio/health/live``, a MinIO-only path: AWS answers 301 there
and the platform refused to start against a real S3 bucket.
"""

from __future__ import annotations

import asyncio
import gc
import threading
import time

import pytest
from minio.error import S3Error

from app.core import startup_checks
from app.services import file_storage as fs


class _Store:
    """A MinIO client stand-in whose bucket check answers or fails as told."""

    def __init__(self, answer, *, delay=0.0, gate=None, make_bucket=None):
        self.answer = answer
        self.delay = delay
        # A test that needs the check to outlast a timeout holds it on ``gate``
        # and releases it itself: no wall-clock race with a loaded worker.
        self.gate = gate
        self.make_bucket_error = make_bucket
        self.calls = 0
        self.builds = 0

    def __call__(self, **_kwargs):
        self.builds += 1
        return self

    def bucket_exists(self, _bucket):
        self.calls += 1
        if self.gate is not None:
            assert self.gate.wait(timeout=30), "the test never released the store"
        time.sleep(self.delay)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def make_bucket(self, _bucket):
        if self.make_bucket_error:
            raise self.make_bucket_error


def _s3_error(code: str) -> S3Error:
    return S3Error(None, code, "message", "resource", "request-id", "host-id")


@pytest.fixture
def storage(monkeypatch):
    service = fs.FileStorageService()
    monkeypatch.setattr(fs, "file_storage", service)
    monkeypatch.setattr(startup_checks, "_storage_probe", None)

    def use(answer, **kwargs) -> _Store:
        store = _Store(answer, **kwargs)
        monkeypatch.setattr(fs, "Minio", store)
        return store

    use.service = service
    return use


@pytest.mark.asyncio
async def test_a_store_that_answers_is_ready(storage):
    storage(True)
    await startup_checks._check_object_storage()


@pytest.mark.asyncio
async def test_an_s3_error_response_still_means_the_store_is_up_and_says_why(storage, caplog):
    storage(_s3_error("AccessDenied"))
    await startup_checks._check_object_storage()
    assert "AccessDenied" in caplog.text


@pytest.mark.asyncio
async def test_a_transport_failure_is_not_ready(storage):
    storage(ConnectionRefusedError("store still booting"))
    with pytest.raises(ConnectionRefusedError):
        await startup_checks._check_object_storage()


def test_a_failed_bucket_check_is_retried_rather_than_cached(storage):
    store = storage(ConnectionRefusedError("store still booting"))
    with pytest.raises(ConnectionRefusedError):
        _ = storage.service.client

    store.answer = True
    assert storage.service.client is store
    assert store.calls == 2


@pytest.mark.asyncio
async def test_a_store_that_does_not_answer_in_time_is_not_ready(storage, monkeypatch):
    monkeypatch.setattr(startup_checks, "OBJECT_STORAGE_TIMEOUT", 0.05)
    gate = threading.Event()
    storage(True, gate=gate)
    try:
        with pytest.raises(TimeoutError):
            await startup_checks._check_object_storage()
    finally:
        gate.set()


def test_a_refused_bucket_check_keeps_the_client(storage):
    store = storage(_s3_error("AccessDenied"))
    with pytest.raises(S3Error):
        _ = storage.service.client

    assert storage.service.client is store
    assert store.calls == 1


def test_a_bucket_another_replica_just_created_is_fine(storage):
    store = storage(False, make_bucket=_s3_error("BucketAlreadyOwnedByYou"))
    assert storage.service.client is store


def test_concurrent_first_calls_build_one_client(storage):
    store = storage(True, delay=0.1)
    threads = [threading.Thread(target=lambda: storage.service.client) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.builds == 1


@pytest.mark.asyncio
async def test_a_timed_out_attempt_is_awaited_again_not_restarted(storage, monkeypatch):
    threads = 0
    to_thread = asyncio.to_thread

    def counted(*args, **kwargs):
        nonlocal threads
        threads += 1
        return to_thread(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", counted)
    monkeypatch.setattr(startup_checks, "OBJECT_STORAGE_TIMEOUT", 0.05)
    gate = threading.Event()
    storage(True, gate=gate)
    try:
        # The store answers only once both attempts gave up: each one times out
        # whatever the worker's load, and the second awaits the first's thread.
        for _ in range(2):
            with pytest.raises(TimeoutError):
                await startup_checks._check_object_storage()
    finally:
        gate.set()
    monkeypatch.setattr(startup_checks, "OBJECT_STORAGE_TIMEOUT", 30.0)
    await startup_checks._check_object_storage()
    assert threads == 1


@pytest.mark.asyncio
async def test_storage_calls_resolve_the_client_off_the_event_loop(storage):
    loop_thread = threading.get_ident()
    store = storage(True)
    seen: list[int] = []
    store.put_object = lambda *a, **k: None
    store.remove_object = lambda *a, **k: None
    original = fs.FileStorageService._new_client

    def tracked():
        seen.append(threading.get_ident())
        return original()

    storage.service._new_client = tracked
    await storage.service.upload(data=b"x", filename="a.txt", content_type="text/plain", prefix="p")
    storage.service._client = None
    await storage.service.delete("p/a.txt")
    assert seen and loop_thread not in seen


@pytest.mark.asyncio
async def test_a_probe_that_fails_after_its_attempt_timed_out_logs_nothing(storage, monkeypatch):
    reported: list[str] = []
    asyncio.get_running_loop().set_exception_handler(lambda _loop, context: reported.append(context["message"]))
    monkeypatch.setattr(startup_checks, "OBJECT_STORAGE_TIMEOUT", 0.05)
    gate = threading.Event()
    storage(ConnectionRefusedError("down"), gate=gate)
    try:
        with pytest.raises(TimeoutError):
            await startup_checks._check_object_storage()
        probe = startup_checks._storage_probe
        startup_checks._storage_probe = None  # the gate gave up
    finally:
        gate.set()  # the probe fails only now, after its attempt timed out
    await asyncio.wait({probe})
    await asyncio.sleep(0)  # let the done callbacks run
    del probe
    gc.collect()
    assert reported == []
