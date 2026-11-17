"""A repeated read of the same job log is answered from the cache (#9098).

GitHub publishes a job's log once, when the job completes, and never changes
it (a re-run is a new job id). Every call of ``get_github_job_logs`` used to
download the whole log again (45 MB, about 7 s for job 127609262804) to give the
same answer. The answer is now kept in Redis per credential digest, repository,
job and read mode; the whole log for the workspace is never cached, and a
Redis that is down only costs the download.
"""

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations import ci_logs, playwright_context
from app.integrations.providers.github import actions, build_tools
from app.integrations.providers.github.actions import _JOB_LOG_CACHE_TTL, _job_log_cache_key

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"
_REDIS = "app.core.redis.redis_client"
_INGEST = "app.integrations.providers.github.actions.workspace_ingest_requested"

_LOG = (
    "##[group]Run pytest\n##[endgroup]\n"
    "=================================== FAILURES ===================================\n"
    "_______________________ test_x _______________________\n"
    "E   AssertionError: boom\n"
    "================== 1 failed, 10 passed in 3.00s ==================\n"
    "##[error]Process completed with exit code 1.\n"
)


def _job_logs_tool():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    fns = {c[0][0]: c[0][1] for c in executor.register.call_args_list}
    return fns["get_github_job_logs"]


class _FakeRedis:
    def __init__(self, *, fail: bool = False):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.fail = fail

    async def get(self, key):
        if self.fail:
            raise ConnectionError("refused")
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("refused")
        self.store[key] = value
        self.ttls[key] = ex


class _GitHub:
    def __init__(self, logs: str = _LOG):
        self.logs = logs
        self.calls = 0

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls += 1
        return httpx.Response(200, text=self.logs), None


def _redis_fn(fake: _FakeRedis):
    async def _client():
        return fake

    return _client


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{}, {"failures_only": True}, {"tail": True}])
async def test_a_repeated_read_is_answered_from_the_cache(kwargs):
    redis, github = _FakeRedis(), _GitHub()
    tool = _job_logs_tool()
    with patch(_REDIS, new=_redis_fn(redis)), patch(_SAFE_REQUEST, new=github):
        first = await tool(repo="o/r", job_id=9, **kwargs)
        second = await tool(repo="o/r", job_id=9, **kwargs)
    assert github.calls == 1
    assert second == first
    assert "boom" in first
    assert list(redis.ttls.values()) == [_JOB_LOG_CACHE_TTL]


@pytest.mark.asyncio
async def test_each_read_mode_and_job_has_its_own_entry():
    redis, github = _FakeRedis(), _GitHub()
    tool = _job_logs_tool()
    with patch(_REDIS, new=_redis_fn(redis)), patch(_SAFE_REQUEST, new=github):
        default = await tool(repo="o/r", job_id=9)
        failures = await tool(repo="o/r", job_id=9, failures_only=True)
        other_job = await tool(repo="o/r", job_id=10)
    assert github.calls == 3
    assert default != failures
    assert "job 10" in other_job
    assert len(redis.store) == 3


def test_the_key_names_no_token_and_differs_per_credential():
    a = _job_log_cache_key({"Authorization": "Bearer secret-a"}, "O", "R", 9, "default")
    b = _job_log_cache_key({"Authorization": "Bearer secret-b"}, "o", "r", 9, "default")
    assert "secret" not in a
    assert a != b
    assert a.endswith(":o/r:9:default")


@pytest.mark.asyncio
async def test_the_workspace_ingest_is_never_cached():
    redis, github = _FakeRedis(), _GitHub()
    tool = _job_logs_tool()
    with patch(_REDIS, new=_redis_fn(redis)), patch(_SAFE_REQUEST, new=github), patch(_INGEST, return_value=True):
        first = await tool(repo="o/r", job_id=9)
        await tool(repo="o/r", job_id=9)
    assert first == _LOG
    assert github.calls == 2
    assert redis.store == {}


@pytest.mark.asyncio
async def test_a_redis_that_is_down_falls_back_to_the_download():
    github = _GitHub()
    tool = _job_logs_tool()
    with patch(_REDIS, new=_redis_fn(_FakeRedis(fail=True))), patch(_SAFE_REQUEST, new=github):
        first = await tool(repo="o/r", job_id=9, failures_only=True)
        second = await tool(repo="o/r", job_id=9, failures_only=True)
    assert "boom" in first and second == first
    assert github.calls == 2


@pytest.mark.asyncio
async def test_an_error_is_never_cached():
    redis = _FakeRedis()

    async def _missing(client, method, url, provider, action, **kwargs):
        return None, "Error: GitHub get logs for job 9 in o/r failed (410): gone"

    tool = _job_logs_tool()
    with patch(_REDIS, new=_redis_fn(redis)), patch(_SAFE_REQUEST, new=_missing):
        out = await tool(repo="o/r", job_id=9)
    assert out.startswith("Error:")
    assert redis.store == {}


def test_the_key_follows_the_code_that_builds_the_answer():
    """A deploy that changes the extractor reads a new entry, never an answer cached before it.

    #12112 changed ``ci_logs`` without bumping a hand-kept version: the retest of
    a failures_only read got the cached answer without the findings section.
    """
    sources = b"".join(
        Path(module.__file__).read_bytes() for module in (ci_logs, playwright_context, actions) if module.__file__
    )
    expected = hashlib.sha256(sources).hexdigest()[:12]
    key = _job_log_cache_key({"Authorization": "Bearer t"}, "o", "r", 9, "failures")
    assert key.startswith(f"github:job_log:{expected}:")
    assert hashlib.sha256(sources + b"# one more line").hexdigest()[:12] != expected
