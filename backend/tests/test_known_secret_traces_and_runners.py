"""Secret variable values out of trace errors and metadata, runner commands, Redis and long-lived tasks.

The surfaces a run reaches past the engine's write hooks: the error and the
metadata a trace keeps, in the durable store and in Langfuse; the command a
Desktop runner is handed and what it sends back; the memory tiers and the
``$collected`` store a turn writes to Redis; and the background loops a run
happens to start, which must not keep its registry for the life of the
process.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.core import redaction
from app.core.redaction import SECRET_MARKER_RE, current_known_secrets, known_secrets_scope, remember_secret_value
from app.services import langfuse_tracing as lt
from app.services import runner_transport

SECRET = "sk-live-TraceSecretValue123456"
VARIABLE_ID = uuid.uuid4()


class _Span:
    """A Langfuse observation that records what it was given."""

    def __init__(self, **created) -> None:
        self.created = created
        self.updates: list[dict] = []
        self.events: list[dict] = []
        self.children: list[_Span] = []

    def start_observation(self, **kwargs) -> _Span:
        child = _Span(**kwargs)
        self.children.append(child)
        return child

    def update(self, **kwargs) -> None:
        self.updates.append(kwargs)

    def end(self) -> None:
        pass

    def create_event(self, **kwargs) -> None:
        self.events.append(kwargs)

    def sent(self) -> str:
        return repr((self.created, self.updates, self.events, [child.sent() for child in self.children]))


def _root(span: _Span) -> dict:
    return {"trace": span, "metadata": {}, "local_trace_id": uuid.uuid4(), "local_spans": [], "local_span_counter": 0}


def test_a_failing_tool_span_keeps_no_secret():
    span = _Span()
    root = _root(span)
    with known_secrets_scope(uuid.uuid4()):
        remember_secret_value(VARIABLE_ID, SECRET)
        tool = lt.start_tool_span(tool_name="http_request", arguments={"token": SECRET}, parent_ctx=root)
        # A failing tool's output is the span's error, in the store and in Langfuse.
        lt.end_tool_span(tool, output=f"401 Unauthorized for token {SECRET}", is_error=True)

    [record] = root["local_spans"]
    assert SECRET not in repr(record)
    assert SECRET_MARKER_RE.search(record["status_message"])
    assert SECRET not in span.sent()


def test_trace_metadata_keeps_no_secret():
    span = _Span()
    root = _root(span)
    with known_secrets_scope(uuid.uuid4()):
        remember_secret_value(VARIABLE_ID, SECRET)
        tool = lt.start_tool_span(tool_name="http_request", parent_ctx=root)
        token = lt._current_trace_ctx.set(tool)
        try:
            lt.annotate_current_tool_span({"resolved_url": f"https://api.example.com/?key={SECRET}"})
        finally:
            lt._current_trace_ctx.reset(token)
        lt.end_tool_span(tool, output="ok")
        lt.enrich_trace_metadata(root, {"note": f"uses {SECRET}"})
        step = lt._start_child_observation(
            root,
            as_type="span",
            name="step",
            span_type="SPAN",
            input_payload=None,
            metadata={"label": SECRET},
            log_label="test",
        )
        lt.end_span(step)

    assert SECRET not in repr({key: value for key, value in root.items() if key != "trace"})
    assert SECRET not in span.sent()
    assert SECRET_MARKER_RE.search(repr(root["local_spans"][0]["metadata"]))


def test_a_failed_trace_keeps_no_secret(monkeypatch):
    from app.services.tracing import dispatcher

    submitted: list = []
    monkeypatch.setattr(dispatcher, "submit_trace_for_flush", lambda trace, spans: submitted.append(trace))
    span = _Span()
    root = _root(span)
    failure = SimpleNamespace(
        cause="tool_error",
        as_trace_metadata=lambda: {"failure_cause": "tool_error", "failure_detail": f"rejected {SECRET}"},
    )
    with known_secrets_scope(uuid.uuid4()):
        remember_secret_value(VARIABLE_ID, SECRET)
        lt.end_trace(root, error=f"HTTP 401: token {SECRET} rejected", failure=failure)

    [trace] = submitted
    assert SECRET not in repr(trace)
    assert SECRET_MARKER_RE.search(trace.error_message)
    assert SECRET not in span.sent()


def test_a_retry_event_keeps_no_secret():
    span = _Span()
    observer = lt.make_retry_attempt_observer({"trace": span})
    with known_secrets_scope(uuid.uuid4()):
        remember_secret_value(VARIABLE_ID, SECRET)
        observer(0, RuntimeError(f"429 for key {SECRET}"), 1.0)

    [event] = span.events
    assert SECRET not in repr(event)
    assert SECRET_MARKER_RE.search(event["status_message"])


async def test_long_lived_loops_keep_no_run_registry(monkeypatch):
    from app.services import browser_tool, relays
    from app.services.tracing.dispatcher import TracingDispatcher

    class _Sink:
        async def close(self) -> None:
            pass

    tracing = TracingDispatcher(sinks=[_Sink()])
    monkeypatch.setattr(browser_tool, "_cleanup_task", None)
    monkeypatch.setattr(relays, "_log_flusher", None)
    monkeypatch.setattr(relays, "_LOG_BUFFER", deque())
    monkeypatch.setattr(relays, "flush_relay_request_logs", AsyncMock(return_value=0))
    with known_secrets_scope(uuid.uuid4()):
        remember_secret_value(VARIABLE_ID, SECRET)
        # Started lazily by the first run of a worker process, as in production.
        await tracing.start()
        await browser_tool._ensure_cleanup_running()
        relays.log_relay_request({"group_id": uuid.uuid4(), "path": f"/v1/items/{SECRET}"})

    loops = [tracing._worker_task, browser_tool._cleanup_task, relays._log_flusher]
    try:
        for task in loops:
            assert task.get_context().get(redaction._known_secrets) is None
        [entry] = relays._LOG_BUFFER
        assert SECRET not in entry["path"]
        assert SECRET_MARKER_RE.search(entry["path"])
    finally:
        await tracing.close(graceful_timeout_seconds=1)
        for task in loops[1:]:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _markers(command_id: uuid.UUID) -> tuple[str, str]:
    """The marker of the variable stored for the command, and one of the run that dispatched it."""
    with known_secrets_scope(command_id) as command:
        own = command.marker(str(VARIABLE_ID))
    with known_secrets_scope(uuid.uuid4()) as run:
        other = run.marker(str(VARIABLE_ID))
    return own, other


def _dispatcher_reads(monkeypatch, value: str | None = SECRET) -> None:
    """The dispatcher's secret variables as they are now: *value*, or none when deleted or unshared."""

    async def dispatcher_variables(_session, _user_id) -> None:
        if value is not None:
            remember_secret_value(VARIABLE_ID, value)

    monkeypatch.setattr(runner_transport, "_load_dispatcher_secrets", dispatcher_variables)


async def test_a_runner_command_restores_its_own_markers_only(monkeypatch):
    _dispatcher_reads(monkeypatch)
    cmd = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())
    own, other = _markers(cmd.id)
    cmd.payload = {"command": f"echo {own} {other}"}

    # Delivered by a drain that runs inside another user's turn (the fast path).
    with known_secrets_scope(uuid.uuid4()) as turn:
        delivered, fingerprints = await runner_transport.restored_command_payload(None, cmd)
        assert not turn, "the dispatcher's values leaked into the turn the drain ran in"

    assert delivered == {"command": f"echo {SECRET} {other}"}
    assert len(fingerprints) == 1 and SECRET not in repr(fingerprints)
    assert current_known_secrets() is None


async def test_a_runner_result_is_stored_with_the_display_text(monkeypatch):
    _dispatcher_reads(monkeypatch)
    cmd = SimpleNamespace(id=uuid.uuid4(), user_id=None, payload={})

    with known_secrets_scope(uuid.uuid4()) as turn:
        status, result, error = await runner_transport._without_secrets(
            None, cmd, "completed", {"stdout": f"token={SECRET}\n", "exit_code": 0}, f"warning: {SECRET} exposed"
        )
        assert not turn

    assert status == "completed"
    assert result == {"stdout": "token=[secret]\n", "exit_code": 0}
    assert error == "warning: [secret] exposed"


async def test_a_runner_payload_without_a_marker_loads_nothing(monkeypatch):
    loaded = AsyncMock()
    monkeypatch.setattr(runner_transport, "_load_dispatcher_secrets", loaded)
    cmd = SimpleNamespace(id=uuid.uuid4(), payload={"command": "ls -la"})

    assert await runner_transport.restored_command_payload(None, cmd) == ({"command": "ls -la"}, set())
    loaded.assert_not_called()


async def _deliver(monkeypatch, cmd: SimpleNamespace, value: str) -> dict:
    """One delivery of *cmd* with the dispatcher's variable holding *value*: what the runner gets."""
    _dispatcher_reads(monkeypatch, value)
    payload, fingerprints = await runner_transport.restored_command_payload(None, cmd)
    runner_transport._record_delivered_secrets(cmd, fingerprints)
    return payload


async def test_a_runner_result_is_kept_while_every_delivered_value_is_still_known(monkeypatch):
    from app.services.runners import DELIVERED_SECRETS_KEY, requested_payload

    cmd = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())
    own, _ = _markers(cmd.id)
    cmd.payload = {"command": f"echo {own}"}
    assert await _deliver(monkeypatch, cmd, SECRET) == {"command": f"echo {SECRET}"}

    # The stored payload names what was delivered without holding it, and the views never show it.
    assert SECRET not in repr(cmd.payload)
    assert cmd.payload[DELIVERED_SECRETS_KEY]
    assert requested_payload(cmd.payload) == {"command": f"echo {own}"}

    status, result, _ = await runner_transport._without_secrets(None, cmd, "completed", {"stdout": SECRET}, None)
    assert (status, result) == ("completed", {"stdout": "[secret]"})


async def test_a_runner_result_echoing_a_value_changed_since_delivery_is_not_kept(monkeypatch):
    rotated = "sk-live-RotatedSecretValue987654"
    for now in (rotated, None):  # rotated, then deleted or unshared (or its vault does not answer)
        cmd = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())
        own, _ = _markers(cmd.id)
        cmd.payload = {"command": f"echo {own}"}
        await _deliver(monkeypatch, cmd, SECRET)

        _dispatcher_reads(monkeypatch, now)
        status, result, error = await runner_transport._without_secrets(
            None, cmd, "completed", {"stdout": f"token={SECRET}"}, f"echoed {SECRET}"
        )

        assert status == "failed" and result is None
        assert SECRET not in error and "not kept" in error


async def test_a_redelivery_after_a_rotation_keeps_both_values_covered(monkeypatch):
    rotated = "sk-live-RotatedSecretValue987654"
    cmd = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())
    own, _ = _markers(cmd.id)
    cmd.payload = {"command": f"echo {own}"}
    await _deliver(monkeypatch, cmd, SECRET)
    # The lease ran out and the command went out again, after the rotation.
    assert await _deliver(monkeypatch, cmd, rotated) == {"command": f"echo {rotated}"}

    # The first run's output may still be the one that lands: the old value is no longer known.
    status, result, _ = await runner_transport._without_secrets(None, cmd, "completed", {"stdout": SECRET}, None)
    assert (status, result) == ("failed", None)


async def test_the_delivery_check_sees_the_command_the_runner_gets(monkeypatch):
    """A blocked word hidden behind a marker is refused: the policy checks the restored command."""
    from app.models.runner import PERMISSION_MODE_RESTRICTED

    blocked_value = "shutdown"
    _dispatcher_reads(monkeypatch, blocked_value)
    monkeypatch.setattr(runner_transport, "_emit_command_terminal", AsyncMock())
    monkeypatch.setattr(runner_transport, "_announce_command_terminal", AsyncMock())
    runner_id = uuid.uuid4()
    cmd = SimpleNamespace(
        id=uuid.uuid4(), runner_id=runner_id, user_id=uuid.uuid4(), kind="run_shell",
        status="pending", dispatched_at=None, error=None, completed_at=None,
    )
    own, _ = _markers(cmd.id)
    cmd.payload = {"command": f"echo {own}"}
    runner = SimpleNamespace(
        is_active=True, permission_mode=PERMISSION_MODE_RESTRICTED, capabilities=["run_shell"],
        blocked_shell_commands=["shutdown"], allowed_shell_commands=None, require_approval=False,
        allowed_paths=None, platform="linux",
    )
    queue = [cmd]

    class _Session:
        commit = AsyncMock()
        rollback = AsyncMock()

        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: queue.pop(0) if queue else None)

        async def get(self, _model, _key, **_options):
            return runner

    assert await runner_transport.claim_next_command(_Session(), runner_id) is None
    assert cmd.status == "cancelled" and "blocked-commands" in cmd.error
    assert blocked_value not in repr(cmd.payload)


def test_a_command_with_a_secret_variable_is_never_remembered_in_a_shell_list():
    from app.api.v1.runners import _remember_shell_decision

    command_id = uuid.uuid4()
    own, _ = _markers(command_id)
    runner = SimpleNamespace(allowed_shell_commands=["ls"], blocked_shell_commands=[])
    cmd = SimpleNamespace(id=command_id, kind="run_shell", payload={"command": f"deploy --token {own}"})

    assert _remember_shell_decision(runner, cmd, "allowed_shell_commands") is False
    assert _remember_shell_decision(runner, cmd, "blocked_shell_commands") is False
    assert runner.allowed_shell_commands == ["ls"] and runner.blocked_shell_commands == []


class _Redis:
    """The Redis calls the memory tiers and the ``$collected`` store make."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.values: dict[str, str] = {}
        self._results: list = []

    def pipeline(self) -> _Redis:
        self._results = []
        return self

    def lpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).insert(0, value)
        self._results.append(1)

    def ltrim(self, *_args) -> None:
        self._results.append(True)

    def expire(self, *_args) -> None:
        self._results.append(True)

    def incr(self, *_args) -> None:
        self._results.append(1)

    def exists(self, *_args) -> None:
        self._results.append(0)

    async def execute(self) -> list:
        return self._results

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value

    async def get(self, key: str) -> str | None:
        return self.values.get(key)


async def test_the_memory_tiers_keep_no_secret(monkeypatch):
    from app.services import agent_memory, user_memory

    redis = _Redis()
    monkeypatch.setattr(agent_memory, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(user_memory, "redis_client", AsyncMock(return_value=redis))
    # The second copy straddles the 1,500-character cap.
    summary = f"token {SECRET} " + "x" * 1453 + f" {SECRET}"
    assert summary[1491:1500] == SECRET[:9]
    with known_secrets_scope(uuid.uuid4()):
        remember_secret_value(VARIABLE_ID, SECRET)
        await agent_memory.store_short_term_memory("agent", source="chat", summary=summary, source_type="chat")
        await user_memory.store_user_memory("user", agent_name="Agent", summary=summary)

    stored = repr(redis.lists)
    assert len(redis.lists) == 2
    assert SECRET[:9] not in stored
    assert SECRET_MARKER_RE.search(stored)


async def test_collected_results_keep_no_secret_and_come_back_in_their_conversation(monkeypatch):
    from app.core import redis as core_redis
    from app.services import agent_chat

    redis = _Redis()
    monkeypatch.setattr(core_redis, "redis_client", AsyncMock(return_value=redis))
    collected = {"$collected": [{"agent": "Converter", "result": f"token={SECRET}"}]}
    conversation = str(uuid.uuid4())
    with known_secrets_scope(conversation):
        remember_secret_value(VARIABLE_ID, SECRET)
        await agent_chat._save_session_vars(conversation, collected)

    [stored] = redis.values.values()
    assert SECRET not in stored
    assert SECRET_MARKER_RE.search(stored)

    # The next turn of the conversation, its variables loaded.
    with known_secrets_scope(conversation):
        remember_secret_value(VARIABLE_ID, SECRET)
        assert await agent_chat._load_session_vars(conversation) == collected
