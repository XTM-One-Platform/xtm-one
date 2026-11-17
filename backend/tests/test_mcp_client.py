"""Regression tests for MCP client connection lifecycle cleanup."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from mcp import types as mcp_types
from mcp.shared.exceptions import McpError

from app.tools import mcp_client

# Originals of the Redis-backed circuit-breaker and auth-failure helpers,
# captured before the autouse isolation fixture below stubs the module
# attributes — the fail-open tests exercise the real implementations.
_REAL_IS_MCP_CIRCUIT_OPEN = mcp_client._is_mcp_circuit_open
_REAL_OPEN_MCP_CIRCUIT = mcp_client._open_mcp_circuit
_REAL_CLOSE_MCP_CIRCUIT = mcp_client._close_mcp_circuit
_REAL_RECORD_MCP_AUTH_FAILURE = mcp_client._record_mcp_auth_failure
_REAL_RESET_MCP_AUTH_FAILURES = mcp_client._reset_mcp_auth_failures
_REAL_RECORD_MCP_CONNECTION_FAILURE = mcp_client._record_mcp_connection_failure
_REAL_GET_MCP_FAILURE_HOURS = mcp_client.get_mcp_failure_hours


@pytest.fixture(autouse=True)
def _isolate_circuit_state(monkeypatch):
    """Keep Redis-backed helpers out of every test by default.

    ``MCPConnection.connect()`` checks/opens/closes the circuit breaker
    against the real Redis instance, and ``execute_tool()`` records/resets
    auth failures.  Without these stubs, tests that drive either path to
    the failure threshold would write to a live dev/CI Redis and cause
    flaky failures with pytest-randomly ordering.

    Tests that exercise circuit or auth-failure behaviour override these
    attributes explicitly (``_track_circuit``, ``_track_auth_counters``,
    or a direct ``setattr``); the fail-open tests call the saved
    ``_REAL_*`` originals.
    """

    async def _circuit_closed(_key: str) -> bool:
        return False

    async def _noop(_key: str) -> None:
        return None

    async def _noop_count(_key: str) -> int:
        return 0

    async def _no_hours(_key: str) -> None:
        return None

    monkeypatch.setattr(mcp_client, "_is_mcp_circuit_open", _circuit_closed)
    monkeypatch.setattr(mcp_client, "_open_mcp_circuit", _noop)
    monkeypatch.setattr(mcp_client, "_close_mcp_circuit", _noop)
    monkeypatch.setattr(mcp_client, "_record_mcp_auth_failure", _noop_count)
    monkeypatch.setattr(mcp_client, "_reset_mcp_auth_failures", _noop)
    monkeypatch.setattr(mcp_client, "get_mcp_failure_hours", _no_hours)


@pytest.fixture
def _resolved_stdio_command(monkeypatch):
    """Bypass the PATH-existence check in ``_resolve_stdio_command``.

    The tests in this file stub out ``stdio_client`` itself and don't
    care whether the command resolves to a real binary — they're
    exercising exit-stack / cancellation behaviour on top of the
    stub.  Without this fixture, ``_resolve_stdio_command`` would
    raise ``FileNotFoundError`` for ``"fake-server"`` before
    ``connect()`` ever reaches the stubbed transport, masking the
    bug each test is actually trying to pin.
    """
    monkeypatch.setattr(
        "app.tools.mcp_client.shutil.which",
        lambda cmd: f"/fake/path/{cmd}",
    )


@pytest.mark.asyncio
async def test_mcp_connection_connect_failure_closes_entered_contexts(
    monkeypatch, _resolved_stdio_command
):
    events: list[str] = []

    @asynccontextmanager
    async def fake_stdio_client(**_kwargs):
        events.append("stdio_enter")
        try:
            yield object(), object()
        finally:
            events.append("stdio_exit")

    class FakeSession:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            events.append("session_enter")
            return self

        async def __aexit__(self, *_exc):
            events.append("session_exit")

        async def initialize(self):
            raise RuntimeError("init failed")

    monkeypatch.setattr(mcp_client, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(mcp_client, "ClientSession", FakeSession)

    config = mcp_client.MCPServerRuntime(
        name="test-server",
        transport="stdio",
        command="fake-server",
    )
    connection = mcp_client.MCPConnection(config)

    with pytest.raises(RuntimeError, match="init failed"):
        await connection.connect()

    assert events == ["stdio_enter", "session_enter", "session_exit", "stdio_exit"]


@pytest.mark.asyncio
async def test_mcp_connection_connect_cancelled_closes_entered_contexts(
    monkeypatch, _resolved_stdio_command
):
    """Regression for the cross-task cancel-scope teardown bug.

    Uses Python 3.11+ task-cancellation semantics — `Task.cancel()`
    triggers a pending cancellation that's only observed on the next
    `await` checkpoint — so the test exercises the same code path
    production hits when a parent task cancels an in-flight
    `connect()`. Raising `CancelledError` directly (as a prior version
    of this test did) bypasses the `cancelling()` count machinery that
    `_close_exit_stack_best_effort`'s uncancel/recancel pattern
    actually has to handle.
    """
    events: list[str] = []

    @asynccontextmanager
    async def fake_stdio_client(**_kwargs):
        events.append("stdio_enter")
        try:
            yield object(), object()
        finally:
            events.append("stdio_exit")

    # ``connect_task`` is the future under test — cancelling it (rather
    # than the surrounding pytest test task) gives us real Python 3.11+
    # task-cancellation semantics on the in-flight ``connect()`` call,
    # without leaking the cancellation up to pytest-asyncio's wrapper.
    connect_task: list[asyncio.Task[None]] = []

    class FakeSession:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            events.append("session_enter")
            return self

        async def __aexit__(self, *_exc):
            events.append("session_exit")

        async def initialize(self):
            # Real cancellation semantics: cancel the task running
            # ``connect()`` (not the test task itself) and hit a
            # checkpoint so the cancellation surfaces inside the
            # in-flight ``connect()`` exactly as it would in production
            # when a parent task calls ``Task.cancel()`` on the
            # connect future.
            assert connect_task, "connect_task fixture must be populated"
            connect_task[0].cancel()
            await asyncio.sleep(0)
            raise AssertionError(
                "initialize() should have observed the cancellation at the checkpoint"
            )

    monkeypatch.setattr(mcp_client, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(mcp_client, "ClientSession", FakeSession)

    config = mcp_client.MCPServerRuntime(
        name="test-server",
        transport="stdio",
        command="fake-server",
    )
    connection = mcp_client.MCPConnection(config)

    connect_task.append(asyncio.create_task(connection.connect()))
    # ``asyncio.wait`` (rather than a bare ``await task``) satisfies the
    # static-analysis "statement has no effect" check while still letting
    # the cancellation play out — we then assert on the task's terminal
    # state directly.
    done, _pending = await asyncio.wait(connect_task)
    [completed] = done
    assert completed.cancelled(), (
        "connect() must propagate the parent task's cancellation; "
        f"observed terminal state: cancelled={completed.cancelled()}, "
        f"exception={completed.exception() if not completed.cancelled() else None!r}"
    )

    # Both contexts must be unwound in LIFO order even though the
    # surrounding task was already in a cancelling state when
    # ``_close_exit_stack_best_effort`` ran.  This is the contract the
    # ``uncancel()``/``cancel()`` dance in the helper protects.
    assert events == ["stdio_enter", "session_enter", "session_exit", "stdio_exit"]


# ---------------------------------------------------------------------------
# _connect_error_detail: McpError recognition by its typed error code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,message,expected_match",
    [
        # The SDK's own closed-transport error, whatever its wording.
        (mcp_types.CONNECTION_CLOSED, "Connection closed", True),
        (mcp_types.CONNECTION_CLOSED, "Verbindung geschlossen", True),
        (mcp_types.CONNECTION_CLOSED, "", True),
        # Any other JSON-RPC error stays an error, even worded like a closure.
        (mcp_types.INTERNAL_ERROR, "Connection closed", False),
        (mcp_types.METHOD_NOT_FOUND, "Tool not found", False),
        (mcp_types.INVALID_PARAMS, "EOF", False),
    ],
)
def test_connect_error_detail_mcp_error(code, message, expected_match):
    """_connect_error_detail recognises the closed-transport McpError by its code (#5303)."""
    exc = McpError(mcp_types.ErrorData(code=code, message=message))
    result = mcp_client._connect_error_detail(exc)
    if expected_match:
        assert result is not None, f"Expected detail for McpError({code}, {message!r})"
        assert result.startswith("McpError: ")
    else:
        assert result is None, f"Expected no detail for McpError({code}, {message!r})"


def test_connect_error_detail_mcp_error_inside_exception_group():
    """The closed-transport McpError is found inside the SDK's task-group wrapper."""
    closed = McpError(mcp_types.ErrorData(code=mcp_types.CONNECTION_CLOSED, message="Connection closed"))
    group = ExceptionGroup("unhandled errors in a TaskGroup", [ValueError("x"), closed])
    assert mcp_client._connect_error_detail(group) == "McpError: Connection closed"


def test_connect_error_detail_closure_wording_without_code_is_not_matched():
    """A non-MCP exception worded like a closure is not read as one (no text matching)."""
    assert mcp_client._connect_error_detail(RuntimeError("connection closed")) is None


def test_connect_error_detail_non_mcp_exception_unchanged():
    """Other non-network exceptions are still returned as None."""
    exc = ValueError("unexpected error")
    assert mcp_client._connect_error_detail(exc) is None


def test_connect_error_detail_connection_error_still_works():
    """Standard ConnectionError is still recognised."""
    exc = ConnectionError("refused")
    result = mcp_client._connect_error_detail(exc)
    assert result is not None
    assert "ConnectionError" in result


# ---------------------------------------------------------------------------
# Persistent connection-failure tracking (inside MCPConnection.connect)
#
# These tests exercise the REAL connect() path (fake transports, as in the
# lifecycle tests above) rather than stubbing MCPConnection.connect — the
# tracking lives inside connect() precisely so the lazy cached-tools path
# (execute_tool connecting on first use) is covered by the same counter.
# ---------------------------------------------------------------------------


def _stdio_config(name: str = "test-server", server_id: str | None = "uuid-1234"):
    return mcp_client.MCPServerRuntime(
        name=name,
        transport="stdio",
        command="fake-server",
        server_id=server_id,
    )


@pytest.fixture
def _failing_transport(monkeypatch, _resolved_stdio_command):
    """Fake transports whose session initialize() raises ConnectionError."""

    @asynccontextmanager
    async def fake_stdio_client(**_kwargs):
        yield object(), object()

    class FakeSession:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            pass

        async def initialize(self):
            raise ConnectionError("refused")

    monkeypatch.setattr(mcp_client, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(mcp_client, "ClientSession", FakeSession)


@pytest.fixture
def _succeeding_transport(monkeypatch, _resolved_stdio_command):
    """Fake transports whose session connects and discovers zero tools."""

    @asynccontextmanager
    async def fake_stdio_client(**_kwargs):
        yield object(), object()

    class _EmptyToolList:
        tools: list = []

    class FakeSession:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            pass

        async def initialize(self):
            return None

        async def list_tools(self):
            return _EmptyToolList()

    monkeypatch.setattr(mcp_client, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(mcp_client, "ClientSession", FakeSession)


def _track_counters(monkeypatch, *, count_on_failure: int = 1):
    """Stub the Redis helpers; return (recorded_keys, reset_keys) lists."""
    recorded: list[str] = []
    reset: list[str] = []

    async def fake_record(key: str) -> int:
        recorded.append(key)
        return count_on_failure

    async def fake_reset(key: str) -> None:
        reset.append(key)

    monkeypatch.setattr(mcp_client, "_record_mcp_connection_failure", fake_record)
    monkeypatch.setattr(mcp_client, "_reset_mcp_connection_failures", fake_reset)
    return recorded, reset


@pytest.mark.asyncio
async def test_connect_failure_increments_counter(monkeypatch, _failing_transport):
    recorded, reset = _track_counters(monkeypatch)

    manager = mcp_client.MCPManager()
    with pytest.raises(ConnectionError):
        await manager.connect_server(_stdio_config())

    assert recorded == ["uuid-1234"]
    assert reset == [], "reset must NOT be called on failure"


@pytest.mark.asyncio
async def test_connect_success_resets_counter(monkeypatch, _succeeding_transport):
    recorded, reset = _track_counters(monkeypatch)

    manager = mcp_client.MCPManager()
    tools = await manager.connect_server(_stdio_config(server_id="srv-abc"))

    assert tools == []
    assert reset == ["srv-abc"]
    assert recorded == [], "record must NOT be called on success"


@pytest.mark.asyncio
async def test_connect_failure_escalates_log_at_threshold(
    monkeypatch, caplog, _failing_transport
):
    """An ERROR-level escalation appears once the counter reaches the threshold."""
    import logging

    _track_counters(
        monkeypatch, count_on_failure=mcp_client._MCP_PERSISTENT_FAILURE_THRESHOLD
    )

    manager = mcp_client.MCPManager()
    with caplog.at_level(logging.ERROR, logger="app.tools.mcp_client"), pytest.raises(
        ConnectionError
    ):
        await manager.connect_server(_stdio_config())

    assert any(
        "consecutive" in rec.message and rec.levelno == logging.ERROR
        for rec in caplog.records
    ), "Expected an ERROR-level 'consecutive' message at threshold"


@pytest.mark.asyncio
async def test_connect_failure_below_threshold_no_error_log(
    monkeypatch, caplog, _failing_transport
):
    import logging

    _track_counters(
        monkeypatch, count_on_failure=mcp_client._MCP_PERSISTENT_FAILURE_THRESHOLD - 1
    )

    manager = mcp_client.MCPManager()
    with caplog.at_level(logging.ERROR, logger="app.tools.mcp_client"), pytest.raises(
        ConnectionError
    ):
        await manager.connect_server(_stdio_config())

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert not error_records, "Expected no ERROR log below failure threshold"


@pytest.mark.asyncio
async def test_connect_failure_past_threshold_is_rate_limited(
    monkeypatch, caplog, _failing_transport
):
    """One failure past the threshold must NOT log ERROR again — the
    escalation re-alerts only every _MCP_FAILURE_REALERT_EVERY failures,
    otherwise a multi-day outage reintroduces the per-run ERROR noise."""
    import logging

    _track_counters(
        monkeypatch, count_on_failure=mcp_client._MCP_PERSISTENT_FAILURE_THRESHOLD + 1
    )

    manager = mcp_client.MCPManager()
    with caplog.at_level(logging.ERROR, logger="app.tools.mcp_client"), pytest.raises(
        ConnectionError
    ):
        await manager.connect_server(_stdio_config())

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert not error_records, "Expected no ERROR log between re-alert intervals"


_THRESHOLD = mcp_client._MCP_PERSISTENT_FAILURE_THRESHOLD
_REALERT = mcp_client._MCP_FAILURE_REALERT_EVERY


@pytest.mark.parametrize(
    "consecutive,expected",
    [
        (0, False),  # fail-open sentinel (Redis down) never escalates
        (_THRESHOLD - 2, False),
        (_THRESHOLD - 1, False),
        (_THRESHOLD, True),  # threshold reached — first escalation
        (_THRESHOLD + 1, False),  # rate-limited
        (_THRESHOLD + _REALERT - 1, False),
        (_THRESHOLD + _REALERT, True),  # periodic reminder
        (_THRESHOLD + _REALERT + 1, False),
        (_THRESHOLD + 2 * _REALERT, True),
    ],
)
def test_should_escalate_mcp_failure(consecutive, expected):
    """Escalation fires at the threshold, then every realert interval."""
    assert mcp_client._should_escalate_mcp_failure(consecutive) is expected


@pytest.fixture
def _code_bug_transport(monkeypatch, _resolved_stdio_command):
    """Fake transports whose session initialize() raises a non-connect bug."""

    @asynccontextmanager
    async def fake_stdio_client(**_kwargs):
        yield object(), object()

    class FakeSession:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            pass

        async def initialize(self):
            raise ValueError("schema validation bug")

    monkeypatch.setattr(mcp_client, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(mcp_client, "ClientSession", FakeSession)


@pytest.mark.asyncio
async def test_non_operational_failure_not_recorded(monkeypatch, _code_bug_transport):
    """Code bugs (connect_detail is None) must NOT touch the failure counter —
    they already log a full ERROR traceback, and counting them would emit a
    misleading 'verify endpoint reachability' escalation."""
    recorded, reset = _track_counters(monkeypatch)

    manager = mcp_client.MCPManager()
    with pytest.raises(ValueError):
        await manager.connect_server(_stdio_config())

    assert recorded == [], "non-connect failures must not increment the counter"
    assert reset == []


@pytest.mark.asyncio
async def test_connect_failure_key_falls_back_to_name(monkeypatch, _failing_transport):
    recorded, _ = _track_counters(monkeypatch)

    manager = mcp_client.MCPManager()
    with pytest.raises(ConnectionError):
        await manager.connect_server(_stdio_config(name="my-server", server_id=None))

    assert recorded == ["my-server"]


@pytest.mark.asyncio
async def test_lazy_cached_connect_failure_increments_counter(
    monkeypatch, _failing_transport
):
    """The lazy cached-tools path (execute_tool connecting on first use)
    hits the same counter — this is the path the LearnWorlds incident
    actually exercised, so it must not bypass the escalation."""
    from app.tools.base import ToolCall

    recorded, _ = _track_counters(monkeypatch)

    manager = mcp_client.MCPManager()
    tools = manager.register_cached_server(
        _stdio_config(server_id="lazy-uuid"),
        [{"name": "cached_tool", "description": "", "input_schema": {}}],
    )
    assert [t.name for t in tools] == ["cached_tool"]

    result = await manager.execute_tool(
        ToolCall(id="call-1", name="cached_tool", arguments={})
    )

    assert result.is_error
    assert recorded == ["lazy-uuid"]


# ---------------------------------------------------------------------------
# Redis failure helpers: fail-open behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_mcp_failure_fails_open_on_redis_error():
    """_record_mcp_connection_failure returns 0 (not raises) when Redis is down."""
    with patch("app.core.redis.redis_client", side_effect=RuntimeError("Redis down")):
        result = await mcp_client._record_mcp_connection_failure("some-server")
    assert result == 0


@pytest.mark.asyncio
async def test_reset_mcp_failures_fails_open_on_redis_error():
    """_reset_mcp_connection_failures silently ignores Redis errors."""
    with patch("app.core.redis.redis_client", side_effect=RuntimeError("Redis down")):
        await mcp_client._reset_mcp_connection_failures("some-server")


@pytest.mark.asyncio
async def test_get_mcp_failure_hours_fails_open_on_redis_error():
    """get_mcp_failure_hours returns None (not raises) when Redis is down."""
    with patch("app.core.redis.redis_client", side_effect=RuntimeError("Redis down")):
        assert await _REAL_GET_MCP_FAILURE_HOURS("some-server") is None


class _FakeDurationRedis:
    """Minimal async Redis stub for the first-failure timestamp helpers."""

    def __init__(self, value: str | None) -> None:
        self._value = value
        self.eval_calls: list[tuple] = []

    async def get(self, _key: str) -> str | None:
        return self._value

    async def eval(self, script: str, numkeys: int, *args) -> int:
        self.eval_calls.append((script, numkeys, args))
        return 1


@pytest.mark.asyncio
async def test_get_mcp_failure_hours_returns_none_when_key_absent():
    async def _fake_client():
        return _FakeDurationRedis(None)

    with patch("app.core.redis.redis_client", _fake_client):
        assert await _REAL_GET_MCP_FAILURE_HOURS("some-server") is None


@pytest.mark.asyncio
async def test_get_mcp_failure_hours_computes_whole_hours():
    """Elapsed time is floored to whole hours (3h30m ago -> 3)."""
    import time as _time

    three_and_a_half_hours_ago = _time.time() - (3 * 3600 + 1800)

    async def _fake_client():
        return _FakeDurationRedis(str(three_and_a_half_hours_ago))

    with patch("app.core.redis.redis_client", _fake_client):
        assert await _REAL_GET_MCP_FAILURE_HOURS("some-server") == 3


@pytest.mark.asyncio
async def test_get_mcp_failure_hours_clamps_future_timestamp_to_zero():
    """A clock-skewed future timestamp must yield 0, never a negative."""
    import time as _time

    async def _fake_client():
        return _FakeDurationRedis(str(_time.time() + 7200))

    with patch("app.core.redis.redis_client", _fake_client):
        assert await _REAL_GET_MCP_FAILURE_HOURS("some-server") == 0


@pytest.mark.asyncio
async def test_record_mcp_connection_failure_single_atomic_round_trip():
    """One failure = ONE Redis EVAL covering counter + first-failure timestamp.

    The combined Lua script must (a) increment the consecutive-failure
    counter with TTL, and (b) set the first-failure timestamp on the first
    failure of the window / EXPIRE-refresh it (timestamp preserved) on
    subsequent failures.  A plain ``SET NX EX`` would let the timestamp key
    expire mid-outage for outages longer than the TTL - the exact case the
    duration feature exists for - and a second round-trip would double the
    Redis load in the failure path during sustained multi-agent outages.
    """
    fake = _FakeDurationRedis(None)

    async def _fake_client():
        return fake

    with patch("app.core.redis.redis_client", _fake_client):
        count = await _REAL_RECORD_MCP_CONNECTION_FAILURE("some-server")

    assert count == 1
    assert len(fake.eval_calls) == 1
    script, numkeys, args = fake.eval_calls[0]
    assert script == mcp_client._MCP_RECORD_FAILURE_LUA
    # Counter branch and both timestamp branches must be present: INCR with
    # TTL, EXPIRE-only refresh for an existing key, SET-with-expiry for a
    # missing key.
    assert "INCR" in script
    assert "EXPIRE" in script and "SET" in script and "EXISTS" in script
    assert numkeys == 2
    assert args[0] == f"{mcp_client._MCP_FAILURE_KEY_PREFIX}some-server"
    assert args[1] == f"{mcp_client._MCP_FIRST_FAILURE_KEY_PREFIX}some-server"
    assert args[3] == mcp_client._MCP_FAILURE_KEY_TTL_SECS


# ---------------------------------------------------------------------------
# Duration-aware ERROR escalation (sustained outages)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_failure_escalation_uses_duration_when_sustained(
    monkeypatch, caplog, _failing_transport
):
    """>= 24 h outage: the ERROR log leads with the human-readable duration."""
    import logging

    _track_counters(
        monkeypatch, count_on_failure=mcp_client._MCP_PERSISTENT_FAILURE_THRESHOLD
    )

    async def _fake_hours(_key: str) -> int:
        return 72

    monkeypatch.setattr(mcp_client, "get_mcp_failure_hours", _fake_hours)

    manager = mcp_client.MCPManager()
    with caplog.at_level(logging.ERROR, logger="app.tools.mcp_client"), pytest.raises(
        ConnectionError
    ):
        await manager.connect_server(_stdio_config())

    error_messages = [
        rec.getMessage() for rec in caplog.records if rec.levelno == logging.ERROR
    ]
    assert any(
        "unreachable for 3 days" in msg and "sustained outage" in msg
        for msg in error_messages
    ), f"Expected a duration-aware sustained-outage ERROR, got: {error_messages}"


@pytest.mark.asyncio
async def test_connect_failure_escalation_stays_count_based_below_24h(
    monkeypatch, caplog, _failing_transport
):
    """< 24 h outage: the ERROR log keeps the legacy count-based wording."""
    import logging

    _track_counters(
        monkeypatch, count_on_failure=mcp_client._MCP_PERSISTENT_FAILURE_THRESHOLD
    )

    async def _fake_hours(_key: str) -> int:
        return 5

    monkeypatch.setattr(mcp_client, "get_mcp_failure_hours", _fake_hours)

    manager = mcp_client.MCPManager()
    with caplog.at_level(logging.ERROR, logger="app.tools.mcp_client"), pytest.raises(
        ConnectionError
    ):
        await manager.connect_server(_stdio_config())

    error_messages = [
        rec.getMessage() for rec in caplog.records if rec.levelno == logging.ERROR
    ]
    assert any("consecutive" in msg for msg in error_messages)
    assert not any("sustained outage" in msg for msg in error_messages)


def test_runtime_failure_key_prefers_server_id():
    """failure_key is the stable DB UUID when available, name otherwise."""
    assert _stdio_config().failure_key == "uuid-1234"
    assert _stdio_config(server_id=None).failure_key == "test-server"


# ---------------------------------------------------------------------------
# _build_mcp_error_message: auth/permission and rate-limit root causes
# ---------------------------------------------------------------------------


def test_mcp_error_message_surfaces_auth_root_cause():
    msg = mcp_client._build_mcp_error_message(
        "search_issues",
        Exception(
            "Validation Error: Validation Failed. The listed users and repositories "
            "cannot be searched either because the resources do not exist or you do not have permission to view them."
        ),
        {"properties": {"q": {"type": "string"}}, "required": ["q"]},
    )
    assert "authentication/permission failure" in msg
    assert "re-authorize or rotate the credential" in msg
    assert "fix the parameters before retrying" not in msg


def test_mcp_error_message_surfaces_bare_401():
    msg = mcp_client._build_mcp_error_message(
        "list_pull_requests",
        Exception("GitHub API error: 401 Unauthorized"),
        {"properties": {"repo": {"type": "string"}}, "required": ["repo"]},
    )
    assert "authentication/permission failure" in msg
    assert "fix the parameters before retrying" not in msg


def test_mcp_error_message_company_managed_escalates_to_admin():
    """Company-managed server: remediation is admin-only, never self-service."""
    msg = mcp_client._build_mcp_error_message(
        "search_code",
        Exception("Authentication Failed: Requires authentication"),
        {"properties": {"query": {"type": "string"}}, "required": ["query"]},
        is_company_managed=True,
    )
    assert "authentication/permission failure" in msg
    assert "company-managed, so only an administrator can fix it" in msg
    assert "fix the parameters before retrying" not in msg


def test_mcp_error_message_user_owned_keeps_self_service_wording():
    """User-owned server: no admin escalation — the caller can fix it."""
    msg = mcp_client._build_mcp_error_message(
        "search_code",
        Exception("Authentication Failed: Requires authentication"),
        {"properties": {"query": {"type": "string"}}, "required": ["query"]},
        is_company_managed=False,
    )
    assert "authentication/permission failure" in msg
    assert "re-authorize or rotate the credential" in msg
    assert "administrator" not in msg
    assert "fix the parameters before retrying" not in msg


def test_mcp_error_message_unknown_ownership_stays_conditional():
    """No ownership info (runtime without a DB record): neutral hint only."""
    msg = mcp_client._build_mcp_error_message(
        "search_code",
        Exception("Authentication Failed: Requires authentication"),
        {"properties": {"query": {"type": "string"}}, "required": ["query"]},
    )
    assert "authentication/permission failure" in msg
    assert "re-authorize or rotate the credential" in msg
    assert "if the server is company-managed" in msg
    assert "fix the parameters before retrying" not in msg


def test_mcp_error_message_surfaces_secondary_rate_limit():
    msg = mcp_client._build_mcp_error_message(
        "search_issues",
        Exception(
            "Permission Denied: You have exceeded a secondary rate limit. "
            "Please wait a few minutes before you try again."
        ),
        {"properties": {"q": {"type": "string"}}, "required": ["q"]},
    )
    assert "Detected rate limiting" in msg
    assert "Stop immediate retries" in msg
    # Must NOT be misread as a permanent credential failure.
    assert "authentication/permission failure" not in msg
    assert "fix the parameters before retrying" not in msg


def test_mcp_error_message_surfaces_primary_rate_limit():
    """A plain primary rate limit (429, no permission wording) must get the
    wait guidance, not the default fix-your-parameters guidance."""
    msg = mcp_client._build_mcp_error_message(
        "list_pull_requests",
        Exception("GitHub API error: 429 API rate limit exceeded for installation"),
        {"properties": {"repo": {"type": "string"}}, "required": ["repo"]},
    )
    assert "Detected rate limiting" in msg
    assert "Stop immediate retries" in msg
    assert "authentication/permission failure" not in msg
    assert "fix the parameters before retrying" not in msg


def test_mcp_error_message_keeps_parameter_guidance_for_plain_errors():
    msg = mcp_client._build_mcp_error_message(
        "search_issues",
        Exception("Validation Failed (422): missing required parameter 'q'"),
        {"properties": {"q": {"type": "string"}}, "required": ["q"]},
    )
    assert "fix the parameters before retrying" in msg
    assert "authentication/permission failure" not in msg


class _FakeCallToolResult:
    def __init__(self, text: str) -> None:
        self.content = [SimpleNamespace(text=text, data=None)]


class _FakeCallToolSession:
    """Session stub whose ``call_tool`` raises queued errors, then succeeds."""

    def __init__(self, failures: list[Exception], result_text: str = "row-data"):
        self.calls: list[tuple[str, dict | None]] = []
        self._failures = list(failures)
        self._result_text = result_text

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments) if arguments else arguments))
        if self._failures:
            raise self._failures.pop(0)
        return _FakeCallToolResult(self._result_text)


def _connected_mcp_connection(
    session, config: mcp_client.MCPServerRuntime | None = None
) -> mcp_client.MCPConnection:
    """Build an MCPConnection wired to a fake session.

    Pass *config* when the test cares about config-derived state
    (``_failure_key``, ``is_company_managed``) — constructing from the
    intended config keeps ``_failure_key`` consistent instead of mutating
    ``conn.config`` after the fact.
    """
    conn = mcp_client.MCPConnection(config or _stdio_config())
    conn._session = session
    conn._connected = True
    return conn


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("search_issues", {"q": "repo:acme/api label:bug"}),
        ("hubspot-list-objects", {"objectType": "CAMPAIGN", "limit": 500}),
        ("hubspot_list_objects___acme__hubspot__read", {"object_type": "campaign", "limit": "500"}),
    ],
)
async def test_execute_tool_sends_the_models_arguments_as_they_are(tool, arguments):
    """No tool name earns a rewrite of its arguments before the call."""
    from app.tools.base import ToolCall

    session = _FakeCallToolSession([])
    conn = _connected_mcp_connection(session)

    await conn.execute_tool(ToolCall(id="c-0", name=tool, arguments=dict(arguments)))

    assert session.calls == [(tool, arguments)]


@pytest.mark.asyncio
async def test_execute_tool_hands_the_servers_error_to_the_model_as_it_is():
    """The generic client rewrites no argument and retries nothing on a
    server's error: the server's own message, which names what it
    accepts, reaches the model with the tool's parameter reference."""
    from app.tools.base import ToolCall

    error = (
        "McpError: { \"error\": \"select: column 'return_rate' not found. "
        "Available columns: keyword, sum_traffic\" }"
    )
    session = _FakeCallToolSession([Exception(error)])
    conn = _connected_mcp_connection(session)
    call = ToolCall(id="c-1", name="site-explorer", arguments={"select": ["return_rate"], "limit": 500})

    result = await conn.execute_tool(call)

    assert result.is_error
    assert "Available columns: keyword, sum_traffic" in result.content
    # What reached the server, not only what the caller still holds.
    assert session.calls == [("site-explorer", {"select": ["return_rate"], "limit": 500})]
    assert call.arguments == {"select": ["return_rate"], "limit": 500}


# ---------------------------------------------------------------------------
# execute_tool: cross-run auth failure tracking
# ---------------------------------------------------------------------------


def _track_auth_counters(monkeypatch, *, count_on_failure: int = 1):
    """Stub the auth-failure Redis helpers; return (recorded_keys, reset_keys) lists."""
    recorded: list[str] = []
    reset: list[str] = []

    async def fake_record(key: str) -> int:
        recorded.append(key)
        return count_on_failure

    async def fake_reset(key: str) -> None:
        reset.append(key)

    monkeypatch.setattr(mcp_client, "_record_mcp_auth_failure", fake_record)
    monkeypatch.setattr(mcp_client, "_reset_mcp_auth_failures", fake_reset)
    return recorded, reset


@pytest.mark.asyncio
async def test_execute_tool_auth_failure_increments_counter(monkeypatch):
    """An auth error during execute_tool records a cross-run failure counter."""
    from app.tools.base import ToolCall

    recorded, reset = _track_auth_counters(monkeypatch)
    session = _FakeCallToolSession(
        [Exception("Authentication Failed: Requires authentication")]
    )
    conn = _connected_mcp_connection(
        session, _stdio_config(server_id="srv-github-123")
    )

    result = await conn.execute_tool(
        ToolCall(id="c-10", name="search_code", arguments={})
    )

    assert result.is_error
    assert recorded == ["srv-github-123"]
    assert reset == [], "reset must NOT be called on failure"


@pytest.mark.asyncio
async def test_execute_tool_non_auth_failure_does_not_increment_auth_counter(monkeypatch):
    """A plain (non-auth) tool error must NOT touch the auth failure counter."""
    from app.tools.base import ToolCall

    recorded, reset = _track_auth_counters(monkeypatch)
    session = _FakeCallToolSession([Exception("Internal server error")])
    conn = _connected_mcp_connection(
        session, _stdio_config(server_id="srv-github-123")
    )

    result = await conn.execute_tool(
        ToolCall(id="c-11", name="search_code", arguments={})
    )

    assert result.is_error
    assert recorded == [], "non-auth error must not increment auth counter"
    assert reset == []


@pytest.mark.asyncio
async def test_execute_tool_success_resets_auth_counter(monkeypatch):
    """A successful tool call resets the auth failure counter."""
    from app.tools.base import ToolCall

    recorded, reset = _track_auth_counters(monkeypatch)
    session = _FakeCallToolSession([])
    conn = _connected_mcp_connection(
        session, _stdio_config(server_id="srv-github-123")
    )

    result = await conn.execute_tool(
        ToolCall(id="c-12", name="search_code", arguments={})
    )

    assert not result.is_error
    assert reset == ["srv-github-123"]
    assert recorded == [], "record must NOT be called on success"


@pytest.mark.asyncio
async def test_execute_tool_auth_failure_escalates_log_at_threshold(
    monkeypatch, caplog
):
    """An additional ERROR-level escalation appears when the auth failure count
    reaches the threshold."""
    import logging

    _track_auth_counters(
        monkeypatch,
        count_on_failure=mcp_client._MCP_AUTH_FAILURE_THRESHOLD,
    )
    from app.tools.base import ToolCall

    session = _FakeCallToolSession(
        [Exception("Authentication Failed: Requires authentication")]
    )
    conn = _connected_mcp_connection(
        session,
        mcp_client.MCPServerRuntime(
            name="GitHub Read-only",
            transport="stdio",
            command="fake-server",
            server_id="srv-github-123",
            is_company_managed=True,
        ),
    )

    with caplog.at_level(logging.ERROR, logger="app.tools.mcp_client"):
        await conn.execute_tool(ToolCall(id="c-13", name="search_code", arguments={}))

    assert any(
        "consecutive" in rec.message
        and "credential" in rec.message
        and rec.levelno == logging.ERROR
        for rec in caplog.records
    ), "Expected an ERROR-level credential-rotation escalation at threshold"


@pytest.mark.asyncio
async def test_execute_tool_auth_failure_below_threshold_no_extra_error_log(
    monkeypatch, caplog
):
    """Below the threshold only the standard logger.exception fires — no extra
    escalation ERROR for auth failures."""
    import logging

    _track_auth_counters(
        monkeypatch,
        count_on_failure=mcp_client._MCP_AUTH_FAILURE_THRESHOLD - 1,
    )
    from app.tools.base import ToolCall

    session = _FakeCallToolSession(
        [Exception("Authentication Failed: Requires authentication")]
    )
    conn = _connected_mcp_connection(
        session,
        mcp_client.MCPServerRuntime(
            name="GitHub Read-only",
            transport="stdio",
            command="fake-server",
            server_id="srv-github-123",
        ),
    )

    with caplog.at_level(logging.ERROR, logger="app.tools.mcp_client"):
        await conn.execute_tool(ToolCall(id="c-14", name="search_code", arguments={}))

    escalation_errors = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and "consecutive" in r.message
    ]
    assert not escalation_errors, "Expected no auth-escalation ERROR below threshold"


_AUTH_THRESHOLD = mcp_client._MCP_AUTH_FAILURE_THRESHOLD
_AUTH_REALERT = mcp_client._MCP_AUTH_FAILURE_REALERT_EVERY


@pytest.mark.parametrize(
    "consecutive,expected",
    [
        (0, False),  # fail-open sentinel never escalates
        (_AUTH_THRESHOLD - 2, False),
        (_AUTH_THRESHOLD - 1, False),
        (_AUTH_THRESHOLD, True),  # threshold reached — first escalation
        (_AUTH_THRESHOLD + 1, False),  # rate-limited
        (_AUTH_THRESHOLD + _AUTH_REALERT - 1, False),
        (_AUTH_THRESHOLD + _AUTH_REALERT, True),  # periodic reminder
        (_AUTH_THRESHOLD + _AUTH_REALERT + 1, False),
        (_AUTH_THRESHOLD + 2 * _AUTH_REALERT, True),
    ],
)
def test_should_escalate_mcp_auth_failure(consecutive, expected):
    """Escalation fires at the threshold, then every realert interval."""
    assert mcp_client._should_escalate_mcp_auth_failure(consecutive) is expected


@pytest.mark.asyncio
async def test_record_mcp_auth_failure_fails_open_on_redis_error():
    """_record_mcp_auth_failure returns 0 (not raises) when Redis is down.

    Calls the ``_REAL_*`` original captured at import time — the autouse
    isolation fixture stubs the module attribute for every test.
    """
    with patch("app.core.redis.redis_client", side_effect=RuntimeError("Redis down")):
        result = await _REAL_RECORD_MCP_AUTH_FAILURE("some-server")
    assert result == 0


@pytest.mark.asyncio
async def test_reset_mcp_auth_failures_fails_open_on_redis_error():
    """_reset_mcp_auth_failures silently ignores Redis errors.

    Calls the ``_REAL_*`` original captured at import time — the autouse
    isolation fixture stubs the module attribute for every test.
    """
    with patch("app.core.redis.redis_client", side_effect=RuntimeError("Redis down")):
        await _REAL_RESET_MCP_AUTH_FAILURES("some-server")  # must not raise


# ---------------------------------------------------------------------------
# Circuit-breaker: open/close/check helpers
# ---------------------------------------------------------------------------


def _track_circuit(monkeypatch):
    """Stub the circuit-breaker helpers; return (open_keys, close_keys) lists."""
    opened: list[str] = []
    closed: list[str] = []

    async def fake_open(key: str) -> None:
        opened.append(key)

    async def fake_close(key: str) -> None:
        closed.append(key)

    monkeypatch.setattr(mcp_client, "_open_mcp_circuit", fake_open)
    monkeypatch.setattr(mcp_client, "_close_mcp_circuit", fake_close)
    return opened, closed


@pytest.mark.asyncio
async def test_circuit_opens_at_threshold(monkeypatch, _failing_transport):
    """Circuit is opened when the failure count reaches the threshold."""
    _track_counters(
        monkeypatch, count_on_failure=mcp_client._MCP_PERSISTENT_FAILURE_THRESHOLD
    )
    opened, closed = _track_circuit(monkeypatch)

    manager = mcp_client.MCPManager()
    with pytest.raises(ConnectionError):
        await manager.connect_server(_stdio_config())

    assert opened == ["uuid-1234"], "Circuit must open at failure threshold"
    assert closed == []


@pytest.mark.asyncio
async def test_circuit_opens_above_threshold(monkeypatch, _failing_transport):
    """Circuit is opened for any failure count >= threshold (not just exactly at it)."""
    _track_counters(
        monkeypatch,
        count_on_failure=mcp_client._MCP_PERSISTENT_FAILURE_THRESHOLD + 5,
    )
    opened, _closed = _track_circuit(monkeypatch)

    manager = mcp_client.MCPManager()
    with pytest.raises(ConnectionError):
        await manager.connect_server(_stdio_config())

    assert opened == ["uuid-1234"]


@pytest.mark.asyncio
async def test_circuit_does_not_open_below_threshold(monkeypatch, _failing_transport):
    """Circuit must not open before the threshold is reached."""
    _track_counters(
        monkeypatch,
        count_on_failure=mcp_client._MCP_PERSISTENT_FAILURE_THRESHOLD - 1,
    )
    opened, _closed = _track_circuit(monkeypatch)

    manager = mcp_client.MCPManager()
    with pytest.raises(ConnectionError):
        await manager.connect_server(_stdio_config())

    assert opened == [], "Circuit must not open below threshold"


@pytest.mark.asyncio
async def test_circuit_closed_on_success(monkeypatch, _succeeding_transport):
    """A successful connection closes the circuit breaker."""
    _track_counters(monkeypatch)
    opened, closed = _track_circuit(monkeypatch)

    manager = mcp_client.MCPManager()
    await manager.connect_server(_stdio_config(server_id="srv-xyz"))

    assert closed == ["srv-xyz"], "Circuit must be closed after successful connect"
    assert opened == []


@pytest.mark.asyncio
async def test_circuit_open_skips_connection(monkeypatch, _failing_transport):
    """When the circuit is open connect() raises _MCPCircuitOpenError immediately
    without touching the failure counter."""
    _track_counters(monkeypatch)

    async def circuit_is_open(key: str) -> bool:
        return True

    monkeypatch.setattr(mcp_client, "_is_mcp_circuit_open", circuit_is_open)

    recorded: list[str] = []

    async def fake_record(key: str) -> int:
        recorded.append(key)
        return mcp_client._MCP_PERSISTENT_FAILURE_THRESHOLD

    monkeypatch.setattr(mcp_client, "_record_mcp_connection_failure", fake_record)

    manager = mcp_client.MCPManager()
    with pytest.raises(mcp_client._MCPCircuitOpenError):
        await manager.connect_server(_stdio_config())

    assert recorded == [], "Failure counter must not be incremented for circuit-skipped calls"


@pytest.mark.asyncio
async def test_circuit_open_skips_lazy_connect(monkeypatch, _failing_transport):
    """The lazy cached-tools path also respects the circuit breaker."""
    from app.tools.base import ToolCall

    async def circuit_is_open(key: str) -> bool:
        return True

    monkeypatch.setattr(mcp_client, "_is_mcp_circuit_open", circuit_is_open)

    manager = mcp_client.MCPManager()
    manager.register_cached_server(
        _stdio_config(server_id="lazy-cb"),
        [{"name": "cached_tool", "description": "", "input_schema": {}}],
    )
    result = await manager.execute_tool(ToolCall(id="c-1", name="cached_tool", arguments={}))

    assert result.is_error
    assert "circuit breaker" in result.content.lower()


@pytest.mark.asyncio
async def test_test_connection_bypasses_circuit(monkeypatch, _succeeding_transport):
    """test_connection always attempts the connection even when the circuit is open."""
    _track_counters(monkeypatch)
    _track_circuit(monkeypatch)

    circuit_checked: list[bool] = []

    async def circuit_is_open(key: str) -> bool:
        # Record the check but return open
        circuit_checked.append(True)
        return True

    monkeypatch.setattr(mcp_client, "_is_mcp_circuit_open", circuit_is_open)

    manager = mcp_client.MCPManager()
    result = await manager.test_connection(_stdio_config(server_id="test-srv"))

    # test_connection must succeed despite the circuit being open (force=True)
    assert result["status"] == "ok", f"Expected ok, got: {result}"
    # The circuit check is NOT called (force=True bypasses it entirely)
    assert circuit_checked == [], "force=True must bypass the circuit-breaker check"


@pytest.mark.asyncio
async def test_connect_server_force_bypasses_circuit(
    monkeypatch, _succeeding_transport
):
    """connect_server(force=True) reaches the wire even when the circuit is
    open — the admin test-tool endpoint relies on this so an explicit
    user-initiated test is never silently skipped during a cooldown."""
    _track_counters(monkeypatch)
    _track_circuit(monkeypatch)

    circuit_checked: list[bool] = []

    async def circuit_is_open(key: str) -> bool:
        circuit_checked.append(True)
        return True

    monkeypatch.setattr(mcp_client, "_is_mcp_circuit_open", circuit_is_open)

    manager = mcp_client.MCPManager()
    tools = await manager.connect_server(_stdio_config(server_id="force-srv"), force=True)

    assert tools == []
    assert circuit_checked == [], "force=True must bypass the circuit-breaker check"


# ---------------------------------------------------------------------------
# Remote-transport URL normalization (inside MCPConnection.connect)
#
# Persisted configs may carry stray whitespace in the URL (same failure mode
# as the stdio command trim) — the connect() path must trim before
# handing the URL to the transport client, and reject URLs that strip to
# empty with the clear "requires a 'url'" config error.
# ---------------------------------------------------------------------------


@pytest.fixture
def _remote_transport(monkeypatch):
    """Fake sse/streamable_http clients that record the URL they receive."""
    seen_urls: list[str] = []

    @asynccontextmanager
    async def fake_remote_client(*, url: str, headers: dict, auth=None, httpx_client_factory=None):
        # Every remote server gets the platform's client factory (for a
        # routed one, the MCP server URL policy for a direct one).
        assert httpx_client_factory is not None
        seen_urls.append(url)
        yield object(), object()

    class _EmptyToolList:
        tools: list = []

    class FakeSession:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            pass

        async def initialize(self):
            return None

        async def list_tools(self):
            return _EmptyToolList()

    monkeypatch.setattr(mcp_client, "sse_client", fake_remote_client)
    monkeypatch.setattr(mcp_client, "streamablehttp_client", fake_remote_client)
    monkeypatch.setattr(mcp_client, "ClientSession", FakeSession)
    return seen_urls


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sse", "streamable_http"])
async def test_connect_trims_remote_url(transport, _remote_transport):
    config = mcp_client.MCPServerRuntime(
        name="remote-server",
        transport=transport,
        url="  https://mcp.example.com/mcp  ",
    )
    connection = mcp_client.MCPConnection(config)

    await connection.connect()

    assert _remote_transport == ["https://mcp.example.com/mcp"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sse", "streamable_http"])
@pytest.mark.parametrize(
    # Non-string values (possible via untyped JSON import bundles) must be
    # treated as missing, not crash with AttributeError on .strip().
    "raw_url",
    [None, "", "   ", 123, ["https://mcp.example.com"]],
)
async def test_connect_rejects_blank_remote_url(transport, raw_url, _remote_transport):
    config = mcp_client.MCPServerRuntime(
        name="remote-server",
        transport=transport,
        url=raw_url,
    )
    connection = mcp_client.MCPConnection(config)

    with pytest.raises(ValueError, match="requires a 'url'"):
        await connection.connect()

    assert _remote_transport == [], "blank URL must never reach the transport"


@pytest.mark.asyncio
async def test_is_mcp_circuit_open_fails_open_on_redis_error():
    """_is_mcp_circuit_open returns False (fail-open) when Redis is unavailable."""
    with patch("app.core.redis.redis_client", side_effect=RuntimeError("Redis down")):
        result = await _REAL_IS_MCP_CIRCUIT_OPEN("some-server")
    assert result is False


@pytest.mark.asyncio
async def test_open_mcp_circuit_fails_open_on_redis_error():
    """_open_mcp_circuit silently ignores Redis errors."""
    with patch("app.core.redis.redis_client", side_effect=RuntimeError("Redis down")):
        await _REAL_OPEN_MCP_CIRCUIT("some-server")  # must not raise


@pytest.mark.asyncio
async def test_close_mcp_circuit_fails_open_on_redis_error():
    """_close_mcp_circuit silently ignores Redis errors."""
    with patch("app.core.redis.redis_client", side_effect=RuntimeError("Redis down")):
        await _REAL_CLOSE_MCP_CIRCUIT("some-server")  # must not raise
