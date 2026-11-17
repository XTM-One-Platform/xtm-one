"""The S3/MinIO client must be built with bounded connect/read timeouts.

The MinIO SDK is synchronous and is driven via ``asyncio.to_thread``; without a
timeout a slow/blackholed object store blocks a default-executor thread forever,
which (because asyncpg resolves DNS on that same executor) starves DB
connection establishment and wedges the worker.
"""

from __future__ import annotations

import urllib3

from app.core.config import settings
from app.services import file_storage as fs


def test_minio_client_built_with_urllib3_timeout(monkeypatch):
    captured: dict = {}

    class _FakePoolManager:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class _FakeMinio:
        def __init__(self, **kwargs):
            self._kwargs = kwargs

        def bucket_exists(self, _bucket):
            return True

    monkeypatch.setattr(urllib3, "PoolManager", _FakePoolManager)
    monkeypatch.setattr(fs, "Minio", _FakeMinio)

    svc = fs.FileStorageService()
    # Touch the lazy ``client`` property to trigger construction.
    _ = svc.client

    assert "timeout" in captured, "MinIO PoolManager built without a timeout"
    timeout = captured["timeout"]
    assert isinstance(timeout, urllib3.Timeout)
    assert timeout.connect_timeout == settings.s3_connect_timeout
    assert timeout.read_timeout == settings.s3_read_timeout
