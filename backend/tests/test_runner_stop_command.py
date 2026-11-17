"""Stop from the card: the control signal that reaches the runner.

``POST /runners/commands/{id}/stop`` publishes ``stop:<command id>`` on the
runner's control channel; the replica holding the runner's socket turns it
into a ``stop`` frame instead of closing the socket (which is what any other
control signal, a revoke, does). These pin that split and the route's guards.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.services import runner_transport


def test_a_stop_signal_carries_its_command_id():
    signal = runner_transport.stop_signal("cmd-1")
    assert runner_transport.stop_frame(signal) == {"type": "stop", "command_id": "cmd-1"}
    assert runner_transport.stop_frame(signal.encode()) == {"type": "stop", "command_id": "cmd-1"}


@pytest.mark.parametrize("signal", ["revoke", b"revoke", "stop:", None, 42])
def test_any_other_signal_is_not_a_stop(signal):
    assert runner_transport.stop_frame(signal) is None


class _PubSub:
    def __init__(self, messages):
        self._messages = list(messages)

    async def subscribe(self, *_channels):
        return None

    async def unsubscribe(self, *_channels):
        return None

    async def aclose(self):
        return None

    async def get_message(self, **_kwargs):
        if self._messages:
            return self._messages.pop(0)
        await asyncio.sleep(0)
        return None


def _listen(monkeypatch, messages, *, rounds_alive: int = 50):
    pubsub = _PubSub(messages)
    client = SimpleNamespace(pubsub=lambda: pubsub, aclose=AsyncMock())
    monkeypatch.setattr(runner_transport, "redis_pubsub_client", AsyncMock(return_value=client))
    monkeypatch.setattr(runner_transport, "_drain_and_push", AsyncMock())
    websocket = MagicMock()
    websocket.close = AsyncMock()
    conn = runner_transport.RunnerConnection("r-1", websocket)
    sent: list[dict] = []

    async def send(message):
        sent.append(message)
        return True

    conn.send = send  # type: ignore[method-assign]

    async def run():
        task = asyncio.create_task(runner_transport._wake_listener(conn, "r-1"))
        for _ in range(rounds_alive):
            await asyncio.sleep(0)
            if task.done():
                break
        conn.alive = False
        await asyncio.wait_for(task, 6)

    asyncio.run(run())
    return conn, websocket, sent


def test_a_stop_signal_reaches_the_runner_and_keeps_the_socket(monkeypatch):
    control = "runner:control:r-1"
    _, websocket, sent = _listen(
        monkeypatch, [{"channel": control, "data": runner_transport.stop_signal("cmd-9")}]
    )
    assert sent == [{"type": "stop", "command_id": "cmd-9"}]
    websocket.close.assert_not_awaited()


def test_a_revoke_still_closes_the_socket(monkeypatch):
    control = "runner:control:r-1"
    _, websocket, sent = _listen(monkeypatch, [{"channel": control, "data": "revoke"}])
    assert sent == []
    websocket.close.assert_awaited_once_with(code=4401)


# ── The route ────────────────────────────────────────────────────────────────


class _Db:
    def __init__(self, cmd):
        self._cmd = cmd
        self.commit = AsyncMock()

    async def get(self, _model, _id):
        return self._cmd


def _route(monkeypatch, cmd, *, manages: bool = False):
    from app.api.v1 import runners as runners_api

    runner = SimpleNamespace(id=uuid.uuid4(), name="desk")
    monkeypatch.setattr(runners_api, "_require_enabled", lambda: None)
    monkeypatch.setattr(runners_api, "_command_runner", AsyncMock(return_value=(runner, [])))
    monkeypatch.setattr(runners_api, "can_manage", lambda *_a, **_k: manages)
    monkeypatch.setattr(runners_api, "log_audit", AsyncMock())
    published = AsyncMock()
    monkeypatch.setattr(runner_transport, "publish_runner_control", published)
    return runners_api, runner, published


def _cmd(status: str, owner: uuid.UUID, kind: str = "run_shell"):
    return SimpleNamespace(
        id=uuid.uuid4(), kind=kind, status=status, user_id=owner, runner_id=uuid.uuid4(), payload={"command": "sleep 60"}
    )


def _user(user_id: uuid.UUID, name: str = "Ada"):
    return SimpleNamespace(id=user_id, full_name=name)


def test_a_background_job_answered_completed_is_still_stopped(monkeypatch):
    # A background run_shell is answered ``completed`` once started and
    # keeps running on the machine under its command id: Stop still reaches it.
    owner = uuid.uuid4()
    cmd = _cmd("completed", owner)
    cmd.payload = {"command": "sleep 600", "background": True}
    api, _, published = _route(monkeypatch, cmd)
    answer = asyncio.run(api.stop_command(str(cmd.id), MagicMock(), _user(owner), _Db(cmd)))
    assert answer["stopping"] is True
    assert answer["status"] == "completed"
    assert cmd.payload["_stopped_by"] == {"user_id": str(owner), "name": "Ada"}
    published.assert_awaited_once()


def test_the_owner_stops_a_running_command(monkeypatch):
    owner = uuid.uuid4()
    cmd = _cmd("dispatched", owner)
    api, runner, published = _route(monkeypatch, cmd)
    answer = asyncio.run(api.stop_command(str(cmd.id), MagicMock(), _user(owner), _Db(cmd)))
    assert answer["stopping"] is True
    published.assert_awaited_once_with(runner.id, f"stop:{cmd.id}")


def test_who_stopped_it_is_kept_on_the_row_before_the_signal_leaves(monkeypatch):
    # The runner's report can come back at once and its result reads who
    # stopped it from the row, so the row is committed before the signal.
    from app.services.runners import STOPPED_BY_KEY, requested_payload

    owner = uuid.uuid4()
    cmd = _cmd("dispatched", owner)
    api, _, published = _route(monkeypatch, cmd)
    db = _Db(cmd)
    order: list[str] = []
    db.commit.side_effect = lambda: order.append("commit")
    published.side_effect = lambda *_a: order.append("signal")
    asyncio.run(api.stop_command(str(cmd.id), MagicMock(), _user(owner, "Grace"), db))
    assert cmd.payload[STOPPED_BY_KEY] == {"user_id": str(owner), "name": "Grace"}
    assert order == ["commit", "signal"]
    # Bookkeeping, not what was asked: never delivered to the runner nor shown.
    assert requested_payload(cmd.payload) == {"command": "sleep 60"}


def test_a_command_that_already_ended_is_not_stopped(monkeypatch):
    owner = uuid.uuid4()
    cmd = _cmd("completed", owner)
    api, _, published = _route(monkeypatch, cmd)
    answer = asyncio.run(api.stop_command(str(cmd.id), MagicMock(), _user(owner), _Db(cmd)))
    assert answer == {"ok": True, "stopping": False, "status": "completed"}
    published.assert_not_awaited()


def test_someone_else_cannot_stop_it_without_managing_the_runner(monkeypatch):
    cmd = _cmd("dispatched", uuid.uuid4())
    api, _, published = _route(monkeypatch, cmd, manages=False)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(api.stop_command(str(cmd.id), MagicMock(), _user(uuid.uuid4()), _Db(cmd)))
    assert caught.value.status_code == 403
    published.assert_not_awaited()


def test_a_runner_manager_can_stop_it(monkeypatch):
    cmd = _cmd("dispatched", uuid.uuid4())
    api, _, published = _route(monkeypatch, cmd, manages=True)
    answer = asyncio.run(api.stop_command(str(cmd.id), MagicMock(), _user(uuid.uuid4()), _Db(cmd)))
    assert answer["stopping"] is True
    published.assert_awaited_once()


@pytest.mark.parametrize("cmd", [None, "not-a-shell"])
def test_only_a_shell_command_can_be_stopped(monkeypatch, cmd):
    owner = uuid.uuid4()
    row = None if cmd is None else _cmd("dispatched", owner, kind="read_file")
    api, _, published = _route(monkeypatch, row)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(api.stop_command(str(uuid.uuid4()), MagicMock(), _user(owner), _Db(row)))
    assert caught.value.status_code == 404
    published.assert_not_awaited()


# ── The result carries who stopped it ────────────────────────────────────────


def _stored(payload: dict | None):
    return SimpleNamespace(payload=payload)


def test_a_stopped_result_carries_who_asked():
    from app.services.runners import STOPPED_BY_KEY

    cmd = _stored({"command": "x", STOPPED_BY_KEY: {"user_id": "u-1", "name": "Ada"}})
    result = runner_transport._with_stopped_by(cmd, {"exit_code": None, "stopped": "user", "stdout": "a"})
    assert result == {"exit_code": None, "stopped": "user", "stdout": "a", "stopped_by": {"user_id": "u-1", "name": "Ada"}}


def test_a_runner_never_chooses_who_stopped_it():
    forged = {"exit_code": None, "stopped": "user", "stopped_by": {"user_id": "u-evil", "name": "Mallory"}}
    assert "stopped_by" not in runner_transport._with_stopped_by(_stored({"command": "x"}), forged)


@pytest.mark.parametrize("result", [None, {"exit_code": 0, "stdout": "done"}])
def test_a_result_that_was_not_stopped_is_unchanged(result):
    from app.services.runners import STOPPED_BY_KEY

    cmd = _stored({STOPPED_BY_KEY: {"user_id": "u-1", "name": "Ada"}})
    assert runner_transport._with_stopped_by(cmd, result) == result
