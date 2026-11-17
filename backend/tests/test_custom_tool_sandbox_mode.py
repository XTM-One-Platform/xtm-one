"""Tests for the custom tool sandbox mode selection (SEC-4).

The CUSTOM_TOOL_SANDBOX setting (auto|docker|subprocess) controls how
user-defined custom tools are isolated:

  - ``auto`` (default): probe Docker availability (positive results cached
    for the process lifetime, negative results re-probed after a short
    interval) and prefer the hardened Docker sandbox; fall back to a plain
    subprocess with a WARNING said once for the whole platform a day when
    Docker is unavailable.
  - ``docker``: require Docker; fail tool execution with a clear error
    when unavailable (no silent fallback).
  - ``subprocess``: always run in a plain subprocess (the same WARNING).

The legacy ``CUSTOM_TOOL_DOCKER=true`` env var is honoured as ``docker``
only when the new setting is not explicitly configured; an explicit value
(including an explicit ``auto``) always wins.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.core import log_once
from app.core.config import settings
from app.services import custom_tool_executor as cte
from tests.notice_store import new_process, share_notices


@pytest.fixture(autouse=True)
def _reset_sandbox_state(monkeypatch):
    """Reset process-wide caches and config between tests."""
    monkeypatch.setattr(cte, "_docker_available", None)
    monkeypatch.setattr(cte, "_docker_probe_failed_at", 0.0)
    # Hermetic default: pretend the Node permission-model probe already ran
    # and found no support, so no real `node` subprocess is spawned. Tests
    # that exercise the resolver reset the cache to None themselves.
    monkeypatch.setattr(cte, "_node_permission_flags", ())
    cte._warn_legacy_env_once.cache_clear()
    monkeypatch.delenv("CUSTOM_TOOL_DOCKER", raising=False)
    monkeypatch.setattr(settings, "custom_tool_sandbox", "auto")
    monkeypatch.setattr(settings, "disable_custom_tools", False)
    # Pydantic marks fields assigned via setattr as explicitly set; the
    # baseline for these tests is "left at the default", so drop the field
    # via the public ``model_fields_set`` view (a live set in Pydantic v2).
    # Tests that need an explicit value re-assign it (setattr re-adds the
    # field to model_fields_set).
    settings.model_fields_set.discard("custom_tool_sandbox")


def _record_executions(monkeypatch) -> list[str]:
    """Replace the docker/subprocess JS executors with recording fakes."""
    calls: list[str] = []

    async def fake_docker(js_function, inputs, timeout=300):
        calls.append("docker")
        return "docker-ok"

    async def fake_subprocess(js_function, inputs, timeout=300):
        calls.append("subprocess")
        return "subprocess-ok"

    monkeypatch.setattr(cte, "execute_js_tool_docker", fake_docker)
    monkeypatch.setattr(cte, "execute_js_tool", fake_subprocess)
    return calls


def _set_probe(monkeypatch, available: bool) -> None:
    async def fake_probe():
        return available

    monkeypatch.setattr(cte, "_probe_docker_available", fake_probe)


# ---------------------------------------------------------------------------
# auto mode
# ---------------------------------------------------------------------------


async def test_auto_prefers_docker_when_available(monkeypatch):
    """auto + Docker available -> the Docker sandbox is chosen."""
    _set_probe(monkeypatch, True)
    calls = _record_executions(monkeypatch)

    result = await cte.execute_js_tool_auto("console.log('hi');", {})

    assert result == "docker-ok"
    assert calls == ["docker"]


async def test_auto_falls_back_to_subprocess_with_warning(monkeypatch, caplog):
    """auto + Docker unavailable -> subprocess fallback + one-time warning."""
    _set_probe(monkeypatch, False)
    calls = _record_executions(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="app.services.custom_tool_executor"):
        result1 = await cte.execute_js_tool_auto("console.log('hi');", {})
        result2 = await cte.execute_js_tool_auto("console.log('hi');", {})

    assert result1 == "subprocess-ok"
    assert result2 == "subprocess-ok"
    assert calls == ["subprocess", "subprocess"]

    isolation_warnings = [
        r for r in caplog.records if "WITHOUT" in r.getMessage()
    ]
    assert len(isolation_warnings) == 1, "Isolation warning must be logged once"
    assert isolation_warnings[0].levelno == logging.WARNING


async def test_repeated_executions_log_debug_breadcrumb_only(monkeypatch, caplog):
    """After the first notice, repeats are a short DEBUG line, never the full text.

    Regression test for the production log flood: on a Docker-less host
    (e.g. Kubernetes without Docker-in-Docker) every custom tool execution
    re-emitted the full multi-line SECURITY notice. Across two executions
    the full message must appear exactly once; the second execution may only
    leave a one-line DEBUG breadcrumb.
    """
    _set_probe(monkeypatch, False)
    calls = _record_executions(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger="app.services.custom_tool_executor"):
        await cte.execute_js_tool_auto("console.log('hi');", {})
        await cte.execute_js_tool_auto("console.log('hi');", {})

    assert calls == ["subprocess", "subprocess"]
    full_notices = [r for r in caplog.records if "SECURITY" in r.getMessage()]
    assert len(full_notices) == 1, "Full security notice must be logged exactly once"
    assert full_notices[0].levelno == logging.WARNING

    breadcrumbs = [
        r
        for r in caplog.records
        if "security notice already logged" in r.getMessage()
    ]
    assert len(breadcrumbs) == 1, "Second execution must leave one DEBUG breadcrumb"
    assert breadcrumbs[0].levelno == logging.DEBUG


async def test_docker_unavailable_probe_log_is_info_once_then_debug(monkeypatch, caplog):
    """The `docker info` failure line is INFO once, DEBUG on later re-probes.

    Negative probe results are re-probed after _DOCKER_PROBE_RETRY_INTERVAL;
    on a permanently Docker-less host that would repeat the INFO line forever
    under steady custom-tool traffic. The prior cached probe state acts as
    the latch (no dedicated global).
    """

    class _FakeProcess:
        async def wait(self):
            return 1

    async def fake_exec(*args, **kwargs):
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with caplog.at_level(logging.DEBUG, logger="app.services.custom_tool_executor"):
        assert await cte._probe_docker_available() is False
        # Pretend the retry interval elapsed so the second call re-probes.
        monkeypatch.setattr(
            cte,
            "_docker_probe_failed_at",
            cte._docker_probe_failed_at - cte._DOCKER_PROBE_RETRY_INTERVAL - 1,
        )
        assert await cte._probe_docker_available() is False

    probe_logs = [
        r for r in caplog.records if "Docker is not available" in r.getMessage()
    ]
    assert [r.levelno for r in probe_logs] == [logging.INFO, logging.DEBUG]


# ---------------------------------------------------------------------------
# docker mode
# ---------------------------------------------------------------------------


async def test_docker_mode_uses_docker(monkeypatch):
    """docker mode + Docker available -> Docker sandbox, no fallback."""
    monkeypatch.setattr(settings, "custom_tool_sandbox", "docker")
    _set_probe(monkeypatch, True)
    calls = _record_executions(monkeypatch)

    result = await cte.execute_js_tool_auto("console.log('hi');", {})

    assert result == "docker-ok"
    assert calls == ["docker"]


async def test_docker_mode_fails_clearly_when_unavailable(monkeypatch):
    """docker mode + Docker unavailable -> clear error, no silent fallback."""
    monkeypatch.setattr(settings, "custom_tool_sandbox", "docker")
    _set_probe(monkeypatch, False)
    calls = _record_executions(monkeypatch)

    with pytest.raises(RuntimeError, match=r"Docker is not.*available"):
        await cte.execute_js_tool_auto("console.log('hi');", {})

    assert calls == [], "No executor must run when Docker is required but missing"


# ---------------------------------------------------------------------------
# subprocess mode
# ---------------------------------------------------------------------------


async def test_subprocess_mode_warns_and_runs_subprocess(monkeypatch, caplog):
    """subprocess mode -> subprocess execution with the one-time warning."""
    monkeypatch.setattr(settings, "custom_tool_sandbox", "subprocess")
    calls = _record_executions(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="app.services.custom_tool_executor"):
        result = await cte.execute_js_tool_auto("console.log('hi');", {})

    assert result == "subprocess-ok"
    assert calls == ["subprocess"]
    assert any("WITHOUT" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# legacy CUSTOM_TOOL_DOCKER env var
# ---------------------------------------------------------------------------


async def test_legacy_env_is_honoured_as_docker_mode(monkeypatch, caplog):
    """CUSTOM_TOOL_DOCKER=true + default setting -> docker mode + deprecation log."""
    monkeypatch.setenv("CUSTOM_TOOL_DOCKER", "true")
    _set_probe(monkeypatch, True)
    calls = _record_executions(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="app.services.custom_tool_executor"):
        result = await cte.execute_js_tool_auto("console.log('hi');", {})

    assert result == "docker-ok"
    assert calls == ["docker"]
    assert any("deprecated" in r.getMessage() for r in caplog.records)


async def test_legacy_env_requires_docker_no_fallback(monkeypatch):
    """CUSTOM_TOOL_DOCKER=true behaves like docker mode: no silent fallback."""
    monkeypatch.setenv("CUSTOM_TOOL_DOCKER", "true")
    _set_probe(monkeypatch, False)
    calls = _record_executions(monkeypatch)

    with pytest.raises(RuntimeError, match=r"Docker is not.*available"):
        await cte.execute_js_tool_auto("console.log('hi');", {})

    assert calls == []


async def test_explicit_setting_wins_over_legacy_env(monkeypatch):
    """An explicit CUSTOM_TOOL_SANDBOX=subprocess overrides the legacy env."""
    monkeypatch.setenv("CUSTOM_TOOL_DOCKER", "true")
    monkeypatch.setattr(settings, "custom_tool_sandbox", "subprocess")
    calls = _record_executions(monkeypatch)

    result = await cte.execute_js_tool_auto("console.log('hi');", {})

    assert result == "subprocess-ok"
    assert calls == ["subprocess"]


async def test_explicit_auto_wins_over_legacy_env(monkeypatch):
    """An explicit CUSTOM_TOOL_SANDBOX=auto overrides the legacy env var.

    Regression test: the legacy CUSTOM_TOOL_DOCKER=true must not force
    docker mode (no fallback) when the operator explicitly opted back into
    auto-detection -- auto + Docker unavailable falls back to subprocess
    instead of failing.
    """
    monkeypatch.setenv("CUSTOM_TOOL_DOCKER", "true")
    # setattr marks the field as explicitly set in settings.model_fields_set
    monkeypatch.setattr(settings, "custom_tool_sandbox", "auto")
    _set_probe(monkeypatch, False)
    calls = _record_executions(monkeypatch)

    result = await cte.execute_js_tool_auto("console.log('hi');", {})

    assert result == "subprocess-ok"
    assert calls == ["subprocess"], "Explicit auto must not be upgraded to docker"


# ---------------------------------------------------------------------------
# Docker probe
# ---------------------------------------------------------------------------


async def test_probe_is_cached_per_process(monkeypatch):
    """A positive `docker info` probe result is cached for the process lifetime."""
    spawn_count = 0

    class _FakeProcess:
        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        nonlocal spawn_count
        spawn_count += 1
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await cte._probe_docker_available() is True
    assert await cte._probe_docker_available() is True
    assert spawn_count == 1, "Probe result must be cached after the first call"


async def test_probe_failure_counts_as_unavailable(monkeypatch):
    """A crashing probe never raises; it marks Docker as unavailable."""

    async def fake_exec(*args, **kwargs):
        raise OSError("docker binary missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await cte._probe_docker_available() is False
    # Cached negative result
    assert cte._docker_available is False


async def test_probe_nonzero_exit_counts_as_unavailable(monkeypatch):
    """`docker info` exiting non-zero (daemon down) -> unavailable."""

    class _FakeProcess:
        async def wait(self):
            return 1

    async def fake_exec(*args, **kwargs):
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await cte._probe_docker_available() is False


async def test_negative_probe_is_reprobed_after_retry_interval(monkeypatch):
    """Docker coming online after a failed probe is picked up without restart."""
    returncodes = iter([1, 0])  # first probe: daemon down; second: up
    spawn_count = 0

    class _FakeProcess:
        async def wait(self):
            return next(returncodes)

    async def fake_exec(*args, **kwargs):
        nonlocal spawn_count
        spawn_count += 1
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await cte._probe_docker_available() is False
    # Within the retry interval the cached negative result is returned.
    assert await cte._probe_docker_available() is False
    assert spawn_count == 1

    # Pretend the retry interval has elapsed -> re-probe finds Docker up.
    monkeypatch.setattr(
        cte,
        "_docker_probe_failed_at",
        cte._docker_probe_failed_at - cte._DOCKER_PROBE_RETRY_INTERVAL - 1,
    )
    assert await cte._probe_docker_available() is True
    assert spawn_count == 2
    # Positive result is cached for the process lifetime: the next call
    # must not spawn another probe subprocess.
    before_cached_probe = spawn_count
    assert await cte._probe_docker_available() is True
    assert spawn_count == before_cached_probe


async def test_probe_is_single_flight_under_concurrency(monkeypatch):
    """Concurrent callers share one probe instead of spawning many `docker info`."""
    spawn_count = 0
    release = asyncio.Event()

    class _FakeProcess:
        async def wait(self):
            await release.wait()
            return 0

    async def fake_exec(*args, **kwargs):
        nonlocal spawn_count
        spawn_count += 1
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    tasks = [asyncio.create_task(cte._probe_docker_available()) for _ in range(5)]
    await asyncio.sleep(0)  # let all tasks reach the lock
    release.set()
    results = await asyncio.gather(*tasks)

    assert results == [True] * 5
    assert spawn_count == 1, "Only one probe subprocess must be spawned"


# ---------------------------------------------------------------------------
# Effective sandbox reporting (security audit)
# ---------------------------------------------------------------------------


async def test_effective_sandbox_reporting(monkeypatch):
    """get_effective_sandbox reflects mode + Docker availability."""
    _set_probe(monkeypatch, True)
    assert await cte.get_effective_sandbox() == "docker"

    _set_probe(monkeypatch, False)
    assert await cte.get_effective_sandbox() == "subprocess"

    monkeypatch.setattr(settings, "custom_tool_sandbox", "docker")
    assert await cte.get_effective_sandbox() == "docker_unavailable"

    monkeypatch.setattr(settings, "custom_tool_sandbox", "subprocess")
    assert await cte.get_effective_sandbox() == "subprocess"


# ---------------------------------------------------------------------------
# execute_tool_auto (unified dispatcher) honours the same selection
# ---------------------------------------------------------------------------


async def test_execute_tool_auto_python_uses_docker_when_available(monkeypatch):
    """The unified dispatcher routes Python tools through Docker in auto mode."""
    _set_probe(monkeypatch, True)
    calls: list[str] = []

    async def fake_py_docker(code, inputs, timeout=300):
        calls.append("python-docker")
        return "ok"

    async def fake_py_subprocess(code, inputs, timeout=300):
        calls.append("python-subprocess")
        return "ok"

    monkeypatch.setattr(cte, "execute_python_tool_docker", fake_py_docker)
    monkeypatch.setattr(cte, "execute_python_tool", fake_py_subprocess)

    await cte.execute_tool_auto("print('hi')", {}, language="python")

    assert calls == ["python-docker"]


# ---------------------------------------------------------------------------
# warn_sandbox_mode_at_startup — startup security notice
# ---------------------------------------------------------------------------


async def test_startup_warn_docker_available_logs_info(monkeypatch, caplog):
    """Docker available -> INFO confirmation, no security warning."""
    _set_probe(monkeypatch, True)

    with caplog.at_level(logging.INFO, logger="app.services.custom_tool_executor"):
        await cte.warn_sandbox_mode_at_startup()

    assert any("Docker" in r.getMessage() and r.levelno == logging.INFO for r in caplog.records)
    assert not any("SECURITY" in r.getMessage() for r in caplog.records)


async def test_startup_warn_subprocess_fallback_logs_warning(monkeypatch, caplog):
    """auto + Docker unavailable -> exactly ONE security notice, at WARNING level.

    WARNING, not ERROR: the subprocess fallback under auto mode is a
    supported/deliberate operating configuration (e.g. Kubernetes without
    Docker-in-Docker), so an unresolvable ERROR would pollute error-level
    monitoring and alerting on every boot. The hard ERROR is
    reserved for the real misconfiguration (explicit docker mode without
    Docker), covered by
    test_startup_warn_docker_unavailable_mode_docker_logs_error.
    """
    _set_probe(monkeypatch, False)

    with caplog.at_level(logging.WARNING, logger="app.services.custom_tool_executor"):
        await cte.warn_sandbox_mode_at_startup()

    security_records = [r for r in caplog.records if "SECURITY" in r.getMessage()]
    assert len(security_records) == 1, "Security notice must be emitted exactly once"
    assert security_records[0].levelno == logging.WARNING


async def test_startup_warn_explicit_subprocess_mode_logs_warning(monkeypatch, caplog):
    """Explicit subprocess mode -> the startup notice is WARNING, not ERROR."""
    monkeypatch.setattr(settings, "custom_tool_sandbox", "subprocess")

    with caplog.at_level(logging.WARNING, logger="app.services.custom_tool_executor"):
        await cte.warn_sandbox_mode_at_startup()

    security_records = [r for r in caplog.records if "SECURITY" in r.getMessage()]
    assert len(security_records) == 1
    assert security_records[0].levelno == logging.WARNING


async def test_startup_warn_suppresses_per_execution_warning(monkeypatch, caplog):
    """The startup notice takes the day's turn, so the per-execution WARNING is silent."""
    _set_probe(monkeypatch, False)
    calls = _record_executions(monkeypatch)

    # Startup probe fires the WARNING for the day.
    await cte.warn_sandbox_mode_at_startup()

    # Subsequent tool executions must not emit any additional isolation warnings.
    with caplog.at_level(logging.WARNING, logger="app.services.custom_tool_executor"):
        caplog.clear()
        await cte.execute_js_tool_auto("console.log('x');", {})

    isolation_warnings = [r for r in caplog.records if "WITHOUT" in r.getMessage()]
    assert isolation_warnings == [], "Per-execution warning must be suppressed after startup probe"
    assert calls == ["subprocess"]


async def test_the_security_notice_is_said_once_for_the_whole_platform(monkeypatch, caplog):
    """Every uvicorn worker, its recycles, the replicas and the worker run the startup check.

    Latched per process, the notice was said 4 times in 12 s on one pod; the
    Security Audit and the sandbox gauge already report the fallback all along.
    """
    notices = share_notices(monkeypatch)
    _set_probe(monkeypatch, False)

    with caplog.at_level(logging.WARNING, logger="app.services.custom_tool_executor"):
        for _ in range(4):
            new_process()
            await cte.warn_sandbox_mode_at_startup()

    security_records = [r for r in caplog.records if "SECURITY" in r.getMessage()]
    assert len(security_records) == 1
    assert security_records[0].levelno == logging.WARNING
    [claim] = notices.claims()
    assert notices.ttls[claim] == 24 * 60 * 60


async def test_a_process_that_outlives_the_day_says_it_again(monkeypatch, caplog):
    """A per-process latch kept a long-lived process silent for good after its first notice."""
    notices = share_notices(monkeypatch)
    _set_probe(monkeypatch, False)
    clock = SimpleNamespace(value=1000.0)
    monkeypatch.setattr(log_once, "time", SimpleNamespace(monotonic=lambda: clock.value))

    with caplog.at_level(logging.WARNING, logger="app.services.custom_tool_executor"):
        await cte.warn_sandbox_mode_at_startup()
        await cte._warn_subprocess_isolation_once()
        notices.interval_elapsed()
        clock.value += 24 * 60 * 60
        await cte._warn_subprocess_isolation_once()

    security_records = [r.getMessage() for r in caplog.records if "SECURITY" in r.getMessage()]
    assert len(security_records) == 2
    assert security_records[1].endswith("(1 more since the last one)")


@pytest.mark.parametrize(
    "change",
    [
        lambda monkeypatch: monkeypatch.setattr(settings, "custom_tool_sandbox", "subprocess"),
        lambda monkeypatch: monkeypatch.setattr(cte, "_node_permission_flags", ("--permission",)),
    ],
    ids=["sandbox-mode", "node-permission"],
)
async def test_the_notice_is_said_again_once_its_configuration_changes(monkeypatch, caplog, change):
    """What the notice states is part of its key: a restart on another configuration says it at once."""
    share_notices(monkeypatch)
    _set_probe(monkeypatch, False)

    with caplog.at_level(logging.WARNING, logger="app.services.custom_tool_executor"):
        await cte.warn_sandbox_mode_at_startup()
        new_process()
        await cte.warn_sandbox_mode_at_startup()
        change(monkeypatch)
        new_process()
        await cte.warn_sandbox_mode_at_startup()

    assert len([r for r in caplog.records if "SECURITY" in r.getMessage()]) == 2


async def test_startup_warn_docker_unavailable_mode_docker_logs_error(monkeypatch, caplog):
    """docker mode + Docker unavailable -> ERROR about failed sandbox at startup."""
    monkeypatch.setattr(settings, "custom_tool_sandbox", "docker")
    _set_probe(monkeypatch, False)

    with caplog.at_level(logging.ERROR, logger="app.services.custom_tool_executor"):
        await cte.warn_sandbox_mode_at_startup()

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("CUSTOM_TOOL_SANDBOX=docker" in r.getMessage() for r in error_records)


async def test_isolation_warn_survives_cancelled_probe(monkeypatch, caplog):
    """A cancelled Node probe must not use up the notice.

    The probe runs before the notice takes its turn: a caller cancelled in it
    leaves the next call to say the notice, instead of a breadcrumb for a
    notice nobody said.
    """

    async def cancelled_probe():
        raise asyncio.CancelledError

    monkeypatch.setattr(cte, "_resolve_node_permission_flags", cancelled_probe)

    with pytest.raises(asyncio.CancelledError):
        await cte._warn_subprocess_isolation_once()

    # After the cancellation the notice can still be emitted normally.
    async def ok_probe():
        return ()

    monkeypatch.setattr(cte, "_resolve_node_permission_flags", ok_probe)

    with caplog.at_level(logging.WARNING, logger="app.services.custom_tool_executor"):
        await cte._warn_subprocess_isolation_once()

    assert any("SECURITY" in r.getMessage() for r in caplog.records)


async def test_isolation_warn_single_flight_under_concurrency(monkeypatch, caplog):
    """Concurrent callers suspended in the probe must emit exactly one notice.

    The callers the notice is not said for must still leave the per-execution
    DEBUG breadcrumb (docstring contract: every such call is correlatable at
    DEBUG level).
    """
    release = asyncio.Event()

    async def slow_probe():
        await release.wait()
        return ()

    monkeypatch.setattr(cte, "_resolve_node_permission_flags", slow_probe)

    with caplog.at_level(logging.DEBUG, logger="app.services.custom_tool_executor"):
        tasks = [
            asyncio.create_task(cte._warn_subprocess_isolation_once())
            for _ in range(5)
        ]
        await asyncio.sleep(0)  # let all tasks reach the lock
        release.set()
        await asyncio.gather(*tasks)

    security_records = [r for r in caplog.records if "SECURITY" in r.getMessage()]
    assert len(security_records) == 1, "Notice must be emitted exactly once"
    breadcrumbs = [
        r
        for r in caplog.records
        if "security notice already logged" in r.getMessage()
    ]
    assert len(breadcrumbs) == 4, "Each caller the notice was not said for must leave a breadcrumb"
    assert all(r.levelno == logging.DEBUG for r in breadcrumbs)


async def test_startup_warn_noop_when_custom_tools_disabled(monkeypatch, caplog):
    """warn_sandbox_mode_at_startup is a no-op when custom tools are disabled."""
    monkeypatch.setattr(settings, "disable_custom_tools", True)

    with caplog.at_level(logging.DEBUG, logger="app.services.custom_tool_executor"):
        await cte.warn_sandbox_mode_at_startup()

    assert not any("SECURITY" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Node.js permission model (subprocess fallback hardening)
# ---------------------------------------------------------------------------


def _set_node_probe(monkeypatch, supported: frozenset[tuple[str, ...]]) -> list[tuple[str, ...]]:
    """Fake the per-flag-set node probe; returns the list of probed sets."""
    probed: list[tuple[str, ...]] = []

    async def fake_probe(flags):
        probed.append(tuple(flags))
        return tuple(flags) in supported

    monkeypatch.setattr(cte, "_run_node_permission_probe", fake_probe)
    return probed


async def test_node_probe_runs_under_sanitized_env(monkeypatch):
    """The probe must use the same sanitized env as real tool execution.

    With the inherited host environment, vars like NODE_OPTIONS could change
    Node startup and make the probe false-negative (silently disabling the
    permission model) even though tools run fine under the sanitized env —
    and the probe subprocess must not receive secrets either.
    """
    monkeypatch.setenv("NODE_OPTIONS", "--max-old-space-size=64")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    captured_env: dict[str, str] = {}

    class _FakeProcess:
        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await cte._run_node_permission_probe(("--permission",)) is True
    assert captured_env, "The probe must pass an explicit sanitized env"
    assert "NODE_OPTIONS" not in captured_env
    assert "DATABASE_URL" not in captured_env


async def test_node_permission_prefers_allow_net_flag_set(monkeypatch):
    """Node 25+: --permission --allow-net is selected (net must stay allowed)."""
    monkeypatch.setattr(cte, "_node_permission_flags", None)
    _set_node_probe(
        monkeypatch,
        frozenset({("--permission", "--allow-net"), ("--permission",)}),
    )

    flags = await cte._resolve_node_permission_flags()

    assert flags == ("--permission", "--allow-net")


async def test_node_permission_falls_back_to_permission_only(monkeypatch):
    """Node 24: --allow-net is unknown, plain --permission is used."""
    monkeypatch.setattr(cte, "_node_permission_flags", None)
    probed = _set_node_probe(monkeypatch, frozenset({("--permission",)}))

    flags = await cte._resolve_node_permission_flags()

    assert flags == ("--permission",)
    assert probed == [("--permission", "--allow-net"), ("--permission",)]


async def test_node_permission_unsupported_returns_empty(monkeypatch):
    """Node < 24: no supported flag set -> unhardened fallback (empty tuple)."""
    monkeypatch.setattr(cte, "_node_permission_flags", None)
    _set_node_probe(monkeypatch, frozenset())

    assert await cte._resolve_node_permission_flags() == ()


async def test_node_permission_result_is_cached(monkeypatch):
    """The probe runs once per process; later calls hit the cache."""
    monkeypatch.setattr(cte, "_node_permission_flags", None)
    probed = _set_node_probe(
        monkeypatch, frozenset({("--permission", "--allow-net")})
    )

    first = await cte._resolve_node_permission_flags()
    second = await cte._resolve_node_permission_flags()

    assert first == second == ("--permission", "--allow-net")
    assert len(probed) == 1, "Probe must be cached after the first resolution"


async def test_node_permission_disabled_by_setting(monkeypatch):
    """CUSTOM_TOOL_NODE_PERMISSION=false -> no probe, no flags."""
    monkeypatch.setattr(settings, "custom_tool_node_permission", False)
    monkeypatch.setattr(cte, "_node_permission_flags", None)
    probed = _set_node_probe(
        monkeypatch, frozenset({("--permission", "--allow-net")})
    )

    assert await cte._resolve_node_permission_flags() == ()
    assert probed == [], "Disabled setting must not spawn a probe"


async def test_execute_js_tool_passes_permission_flags_to_node(monkeypatch):
    """The resolved permission flags are inserted into the node invocation."""
    monkeypatch.setattr(cte, "_node_permission_flags", ("--permission", "--allow-net"))
    captured_args: list[str] = []

    class _FakeProcess:
        returncode = 0

        async def communicate(self, input=None):
            return b"ok", b""

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await cte.execute_js_tool("console.log('ok');", {})

    assert result == "ok"
    assert captured_args == ["node", "--permission", "--allow-net"]


async def test_execute_js_tool_runs_bare_node_when_unsupported(monkeypatch):
    """Without permission-model support the invocation is unchanged."""
    monkeypatch.setattr(cte, "_node_permission_flags", ())
    captured_args: list[str] = []

    class _FakeProcess:
        returncode = 0

        async def communicate(self, input=None):
            return b"ok", b""

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await cte.execute_js_tool("console.log('ok');", {})

    assert captured_args == ["node"]


async def test_isolation_notice_reflects_node_hardening(monkeypatch, caplog):
    """With the permission model active, the notice must not claim full privileges."""
    monkeypatch.setattr(cte, "_node_permission_flags", ("--permission", "--allow-net"))
    _set_probe(monkeypatch, False)

    with caplog.at_level(logging.WARNING, logger="app.services.custom_tool_executor"):
        await cte.warn_sandbox_mode_at_startup()

    security_records = [r for r in caplog.records if "SECURITY" in r.getMessage()]
    assert len(security_records) == 1
    message = security_records[0].getMessage()
    assert "full Node.js privileges" not in message
    assert "permission model" in message
    # The Python clause must reflect the actual hardening level too
    # (isolated mode -I on top of the module blocklist).
    assert "isolated mode (-I" in message


async def test_isolation_notice_without_node_hardening(monkeypatch, caplog):
    """Without permission-model support the notice keeps the original wording."""
    monkeypatch.setattr(cte, "_node_permission_flags", ())
    _set_probe(monkeypatch, False)

    with caplog.at_level(logging.WARNING, logger="app.services.custom_tool_executor"):
        await cte.warn_sandbox_mode_at_startup()

    security_records = [r for r in caplog.records if "SECURITY" in r.getMessage()]
    assert len(security_records) == 1
    message = security_records[0].getMessage()
    assert "full Node.js privileges" in message
    assert "isolated mode (-I" in message


# ---------------------------------------------------------------------------
# Prometheus gauge — effective sandbox exported for alerting
# ---------------------------------------------------------------------------


def _gauge_value(state: str) -> float:
    return cte.SANDBOX_GAUGE.labels(sandbox=state)._value.get()


async def test_gauge_reflects_docker_sandbox(monkeypatch):
    _set_probe(monkeypatch, True)

    await cte.warn_sandbox_mode_at_startup()

    assert _gauge_value("docker") == 1.0
    assert _gauge_value("subprocess") == 0.0
    assert _gauge_value("docker_unavailable") == 0.0
    assert _gauge_value("disabled") == 0.0


async def test_gauge_reflects_subprocess_fallback(monkeypatch):
    _set_probe(monkeypatch, False)

    await cte.warn_sandbox_mode_at_startup()

    assert _gauge_value("subprocess") == 1.0
    assert _gauge_value("docker") == 0.0


async def test_gauge_reflects_docker_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "custom_tool_sandbox", "docker")
    _set_probe(monkeypatch, False)

    await cte.warn_sandbox_mode_at_startup()

    assert _gauge_value("docker_unavailable") == 1.0
    assert _gauge_value("docker") == 0.0


async def test_gauge_reflects_disabled_custom_tools(monkeypatch):
    monkeypatch.setattr(settings, "disable_custom_tools", True)

    await cte.warn_sandbox_mode_at_startup()

    assert _gauge_value("disabled") == 1.0
    assert _gauge_value("docker") == 0.0
    assert _gauge_value("subprocess") == 0.0
