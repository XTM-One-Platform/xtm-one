"""Runner presence events alternate, and a deploy fires none.

``Runner online`` / ``Runner offline`` are the transitions of the cached
``Runner.status``: a heartbeat (``touch_runner``) flips an offline runner
online, the scheduler's sweep (``sweep_stale_runners``) flips a runner whose
heartbeat went stale offline, and a socket closing moves nothing; enrollment
and revocation fire only on a real transition, and a revoked row is final.
Before this, every deploy closed every runner socket on the old pod, which
fired ``Runner offline`` for each machine, while the reconnect to the new pod
fired no ``Runner online`` (the machine had never looked offline by recency).

Also pinned: an agent tool that stopped waiting for a command nobody picked up
times it out, so it cannot run later with nobody waiting.

The guarded UPDATEs are what make each transition fire once, so they run
against PostgreSQL. Same scratch-database recipe as
``test_a2a_cancel_child_runs_postgres.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.websockets import WebSocketDisconnect

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.core.config import settings
from app.models.runner import Runner, RunnerCommand
from app.services import runner_flow_events, runner_transport
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("presence", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


@pytest.fixture
async def maker(monkeypatch):
    async with _temp_database() as sessions:
        yield sessions


@pytest.fixture
def presence(monkeypatch) -> list[tuple[str, uuid.UUID]]:
    """Record the presence events instead of writing them to the flow bus."""
    events: list[tuple[str, uuid.UUID]] = []

    async def record(_db, runner_id, *, online: bool) -> None:
        events.append(("online" if online else "offline", runner_id))

    monkeypatch.setattr(runner_flow_events, "emit_runner_presence", record)
    monkeypatch.setattr(runner_transport, "_notify_flow_engine", AsyncMock())
    return events


@pytest.fixture
def revoke(monkeypatch):
    """Run the revoke route's handler on a runner the caller already loaded."""
    from app.api.v1 import runners as runners_api

    monkeypatch.setattr(runners_api, "log_audit", AsyncMock())
    monkeypatch.setattr(runners_api, "can_manage", lambda *_a, **_k: True)
    monkeypatch.setattr(runner_transport, "publish_runner_control", AsyncMock())

    async def call(db, runner: Runner) -> None:
        async def visible(*_args, **_kwargs):
            return runner, []

        monkeypatch.setattr(runners_api, "_get_visible_runner", visible)
        await runners_api.revoke_runner(str(runner.id), MagicMock(), MagicMock(), db)

    return call


async def _runner(maker, *, status: str, seen_ago: float, active: bool = True) -> uuid.UUID:
    async with maker() as db:
        runner = Runner(
            name="desk",
            platform="linux",
            key_prefix="run-test",
            key_hash="hash",
            status=status,
            is_active=active,
            last_seen_at=datetime.now(UTC) - timedelta(seconds=seen_ago),
            # The commands below are shell commands, re-authorized at delivery.
            capabilities=["run_shell"],
        )
        db.add(runner)
        await db.commit()
        return runner.id


async def _row(maker, runner_id: uuid.UUID) -> Runner:
    async with maker() as db:
        return await db.get(Runner, runner_id)


async def _status(maker, runner_id: uuid.UUID) -> str:
    return (await _row(maker, runner_id)).status


def _stale() -> float:
    return settings.runner_offline_after_seconds + 30


async def test_a_deploy_reconnect_fires_no_presence_event(maker, presence):
    # The old pod dropped the socket a few seconds ago; the runner is back.
    runner_id = await _runner(maker, status="online", seen_ago=5)
    await runner_transport.touch_runner(runner_id, transport="ws")
    assert await runner_transport.sweep_stale_runners() == 0
    assert presence == []
    assert await _status(maker, runner_id) == "online"


async def test_a_closed_socket_writes_no_presence(maker, presence, monkeypatch):
    """What a deploy does to every connected runner: the socket just closes."""
    runner_id = await _runner(maker, status="online", seen_ago=5)

    async def idle(_conn, _runner_id) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(runner_transport, "_wake_listener", idle)
    monkeypatch.setattr(runner_transport, "_drain_and_push", AsyncMock(return_value=0))
    websocket = MagicMock()
    websocket.receive_json = AsyncMock(side_effect=WebSocketDisconnect())

    await runner_transport.serve_runner_websocket(websocket, SimpleNamespace(id=runner_id))

    assert presence == []
    assert await _status(maker, runner_id) == "online"


async def test_a_stale_runner_goes_offline_once(maker, presence):
    runner_id = await _runner(maker, status="online", seen_ago=_stale())
    assert await runner_transport.sweep_stale_runners() == 1
    assert await runner_transport.sweep_stale_runners() == 0
    assert presence == [("offline", runner_id)]
    assert await _status(maker, runner_id) == "offline"


async def test_a_fresh_runner_is_left_alone(maker, presence):
    await _runner(maker, status="online", seen_ago=10)
    assert await runner_transport.sweep_stale_runners() == 0
    assert presence == []


async def test_a_runner_back_after_going_offline_fires_online_once(maker, presence):
    runner_id = await _runner(maker, status="online", seen_ago=_stale())
    await runner_transport.sweep_stale_runners()
    await runner_transport.touch_runner(runner_id, transport="long_poll")
    await runner_transport.touch_runner(runner_id, transport="long_poll")
    assert presence == [("offline", runner_id), ("online", runner_id)]
    assert await _status(maker, runner_id) == "online"


@pytest.mark.parametrize("cached", ["revoked", "online"])
async def test_a_revoked_runner_is_never_swept(maker, presence, cached):
    # ``online``: a runner revoked by an older build, whose cached status a
    # heartbeat racing the revoke rewrote.
    runner_id = await _runner(maker, status=cached, seen_ago=_stale(), active=False)
    assert await runner_transport.sweep_stale_runners() == 0
    assert presence == []
    assert await _status(maker, runner_id) == cached


async def test_a_heartbeat_from_a_revoked_runner_writes_nothing(maker, presence):
    """A heartbeat that authenticated just before the revoke committed reads
    the revoked row under the lock: it stays revoked, and nothing fires."""
    runner_id = await _runner(maker, status="revoked", seen_ago=_stale(), active=False)
    last_seen = (await _row(maker, runner_id)).last_seen_at
    await runner_transport.touch_runner(runner_id, transport="long_poll")
    row = await _row(maker, runner_id)
    assert presence == []
    assert (row.status, row.last_seen_at) == ("revoked", last_seen)


@pytest.mark.parametrize(("before", "expected"), [("online", ["offline"]), ("offline", [])])
async def test_revoking_a_runner_fires_offline_only_if_it_was_online(
    maker, presence, revoke, before, expected
):
    runner_id = await _runner(maker, status=before, seen_ago=5)
    async with maker() as db:
        await revoke(db, await db.get(Runner, runner_id))
    assert [kind for kind, _ in presence] == expected
    assert await _status(maker, runner_id) == "revoked"


@pytest.mark.parametrize("meanwhile", ["sweep", "revoke"])
async def test_a_revoke_decides_on_the_row_under_its_lock(maker, presence, revoke, meanwhile):
    """The route loaded the runner online; before it wrote, the presence sweep
    (or a second revoke) had already declared the machine gone. One
    "Runner offline", never two."""
    runner_id = await _runner(maker, status="online", seen_ago=_stale())
    async with maker() as db:
        loaded = await db.get(Runner, runner_id)
        if meanwhile == "sweep":
            assert await runner_transport.sweep_stale_runners() == 1
        else:
            async with maker() as other:
                await revoke(other, await other.get(Runner, runner_id))
        await revoke(db, loaded)
    assert presence == [("offline", runner_id)]
    assert await _status(maker, runner_id) == "revoked"


@pytest.mark.parametrize(
    ("existing", "expected"), [(None, ["online"]), ("offline", ["online"]), ("online", [])]
)
async def test_enrolling_fires_online_only_for_a_machine_that_was_not_online(
    maker, presence, monkeypatch, existing, expected
):
    """A new machine, or the re-enrollment of one marked offline, is a
    "Runner online"; re-enrolling a machine that is online is not."""
    from app.api.v1 import runners as runners_api
    from app.models.runner import RunnerEnrollmentKey
    from app.models.user import User

    monkeypatch.setattr(settings, "runners_enabled", True)
    monkeypatch.setattr(runners_api, "log_audit", AsyncMock())
    monkeypatch.setattr(runners_api, "hash_key", AsyncMock(return_value="hash"))
    async with maker() as db:
        user = User(
            email=f"presence-{uuid.uuid4().hex[:8]}@test.local",
            full_name="Presence Test",
            hashed_password="x",
        )
        db.add(user)
        await db.flush()
        key = RunnerEnrollmentKey(
            user_id=user.id, name="fleet", key_prefix="renr-test", key_hash="hash"
        )
        db.add(key)
        if existing is not None:
            db.add(
                Runner(
                    user_id=user.id,
                    name="desk",
                    platform="linux",
                    machine_id="machine-1",
                    key_prefix="run-test",
                    key_hash="hash",
                    status=existing,
                    last_seen_at=datetime.now(UTC) - timedelta(seconds=5),
                )
            )
        await db.commit()
    monkeypatch.setattr(runners_api, "resolve_enrollment_key", AsyncMock(return_value=key))
    body = runners_api.EnrollRequest(
        enrollment_key="renr-test", platform="linux", hostname="desk", machine_id="machine-1"
    )
    async with maker() as db:
        await runners_api.enroll_runner(body, db)
    assert [kind for kind, _ in presence] == expected


async def test_a_sweep_racing_a_heartbeat_keeps_events_and_status_consistent(maker, presence):
    """The sweep can never slip between a heartbeat's read and its write."""
    for _ in range(10):
        presence.clear()
        runner_id = await _runner(maker, status="online", seen_ago=_stale())
        await asyncio.gather(
            runner_transport.sweep_stale_runners(),
            runner_transport.touch_runner(runner_id, transport="ws"),
        )
        kinds = [kind for kind, rid in presence if rid == runner_id]
        # Either the heartbeat won (nothing happened) or the sweep did and
        # the heartbeat brought the runner back: never a lone "offline".
        assert kinds in ([], ["offline", "online"]), kinds
        assert await _status(maker, runner_id) == "online"


async def test_concurrent_heartbeats_fire_online_once(maker, presence):
    runner_id = await _runner(maker, status="offline", seen_ago=_stale())
    await asyncio.gather(
        runner_transport.touch_runner(runner_id, transport="ws"),
        runner_transport.touch_runner(runner_id, transport="long_poll"),
    )
    assert presence == [("online", runner_id)]


async def _command(
    maker,
    runner_id: uuid.UUID,
    status: str,
    *,
    dispatched_ago: float | None = None,
    given_up: bool = False,
    flow_run_id: uuid.UUID | None = None,
) -> uuid.UUID:
    now = datetime.now(UTC)
    async with maker() as db:
        cmd = RunnerCommand(
            runner_id=runner_id,
            kind="run_shell",
            payload={},
            status=status,
            flow_run_id=flow_run_id,
            dispatched_at=(
                now - timedelta(seconds=dispatched_ago) if dispatched_ago is not None else None
            ),
            given_up_at=now if given_up else None,
        )
        db.add(cmd)
        await db.commit()
        return cmd.id


async def _command_row(maker, command_id: uuid.UUID) -> RunnerCommand:
    async with maker() as db:
        return await db.get(RunnerCommand, command_id)


@pytest.fixture
def terminal(monkeypatch) -> list[tuple[uuid.UUID, str]]:
    """Record the ``Runner command completed`` emissions; Redis is unreachable."""
    events: list[tuple[uuid.UUID, str]] = []

    async def record(_db, cmd, _runner) -> None:
        events.append((cmd.id, cmd.status))

    monkeypatch.setattr(runner_flow_events, "emit_runner_command_completed", record)
    monkeypatch.setattr(runner_transport, "redis_client", AsyncMock(side_effect=ConnectionError))
    return events


def _past_lease() -> float:
    return runner_transport._command_lease_seconds() + 30


# ── Giving up on a command ───────────────────────────────────────────


async def test_giving_up_a_queued_command_finishes_it_once(maker, presence, terminal):
    runner_id = await _runner(maker, status="offline", seen_ago=_stale())
    command_id = await _command(maker, runner_id, "pending")
    row = await runner_transport.give_up_on_command(command_id, "nobody picked it up")
    assert (row["status"], row["error"]) == ("timeout", "nobody picked it up")
    stored = await _command_row(maker, command_id)
    assert stored.completed_at is not None and stored.given_up_at is not None
    # A terminal result like any other: flow event + flow engine wake.
    assert terminal == [(command_id, "timeout")]
    runner_transport._notify_flow_engine.assert_awaited()
    # Idempotent: a second give-up changes nothing and fires nothing.
    again = await runner_transport.give_up_on_command(command_id, "again")
    assert again["error"] == "nobody picked it up"
    assert terminal == [(command_id, "timeout")]


async def test_a_command_a_runner_holds_keeps_its_late_result(maker, presence, terminal):
    runner_id = await _runner(maker, status="online", seen_ago=5)
    command_id = await _command(maker, runner_id, "dispatched", dispatched_ago=5)
    row = await runner_transport.give_up_on_command(command_id, "x")
    assert row["status"] == "dispatched"
    assert terminal == []
    assert (await _command_row(maker, command_id)).given_up_at is not None
    # What ran is still recorded - the evidence outlives the waiter.
    assert await runner_transport.submit_result(
        command_id, status="completed", result={"stdout": "done"}
    )
    assert (await _command_row(maker, command_id)).status == "completed"


@pytest.mark.parametrize(("given_up", "expected"), [(False, "pending"), (True, "timeout")])
async def test_a_failed_send_requeues_only_a_command_still_awaited(
    maker, presence, terminal, given_up, expected
):
    runner_id = await _runner(maker, status="online", seen_ago=5)
    command_id = await _command(
        maker, runner_id, "dispatched", dispatched_ago=1, given_up=given_up
    )
    await runner_transport._requeue_command(command_id)
    assert (await _command_row(maker, command_id)).status == expected


async def test_a_lost_command_still_awaited_is_redelivered(maker, presence):
    runner_id = await _runner(maker, status="online", seen_ago=5)
    command_id = await _command(maker, runner_id, "dispatched", dispatched_ago=_past_lease())
    async with maker() as db:
        claimed = await runner_transport.claim_next_command(db, runner_id)
    assert claimed is not None and claimed["id"] == str(command_id)


async def test_the_result_wait_ends_while_the_lease_still_holds_the_command(maker, presence):
    # The default wait runs from the delivery the row records and ends before
    # the lease: at that moment no drain hands the command out again,
    # so the give-up its waiter records is never raced by a redelivery.
    runner_id = await _runner(maker, status="online", seen_ago=5)
    queued = await _command(maker, runner_id, "pending")
    assert (await runner_transport.load_command(queued))["dispatched_at"] is None
    async with maker() as db:
        claimed = await runner_transport.claim_next_command(db, runner_id)
    assert claimed is not None and claimed["id"] == str(queued)
    # The budget the wait is derived from is the one every command is handed.
    assert claimed["timeout"] == settings.runner_command_default_timeout_seconds
    delivered = (await runner_transport.load_command(queued))["dispatched_at"]
    assert delivered.tzinfo is not None
    assert abs((datetime.now(UTC) - delivered).total_seconds()) < 60

    waited = await _command(
        maker,
        runner_id,
        "dispatched",
        dispatched_ago=runner_transport.default_result_wait_seconds(),
    )
    async with maker() as db:
        assert await runner_transport.claim_next_command(db, runner_id) is None
    assert (await _command_row(maker, waited)).status == "dispatched"


async def test_a_given_up_command_is_never_redelivered_and_ends_after_its_lease(
    maker, presence, terminal
):
    runner_id = await _runner(maker, status="online", seen_ago=5)
    expired = await _command(
        maker, runner_id, "dispatched", dispatched_ago=_past_lease(), given_up=True
    )
    fresh = await _command(maker, runner_id, "dispatched", dispatched_ago=5, given_up=True)
    async with maker() as db:
        assert await runner_transport.claim_next_command(db, runner_id) is None
    assert await runner_transport.expire_given_up_commands() == 1
    assert (await _command_row(maker, expired)).status == "timeout"
    assert (await _command_row(maker, fresh)).status == "dispatched"
    assert terminal == [(expired, "timeout")]


async def test_a_flow_run_that_ends_gives_up_its_commands(maker, presence, terminal):
    """Cancelled or dead-lettered: a held command is cancelled (no late approval
    runs it for a run that is gone) and one a runner took is never sent again -
    found by run, so a command committed before the run persisted its park is
    released too (the run below has no parked state). Another run's command is
    left alone."""
    from app.services.flow_runner import release_runner_commands

    runner_id = await _runner(maker, status="online", seen_ago=5)
    run_id = uuid.uuid4()
    held = await _command(maker, runner_id, "pending_approval", flow_run_id=run_id)
    taken = await _command(maker, runner_id, "dispatched", dispatched_ago=5, flow_run_id=run_id)
    other = await _command(maker, runner_id, "pending", flow_run_id=uuid.uuid4())
    async with maker() as db:
        await release_runner_commands(
            db, SimpleNamespace(id=run_id, state={}), reason="flow cancelled"
        )
        await db.commit()
    rows = {name: await _command_row(maker, cid) for name, cid in
            (("held", held), ("taken", taken), ("other", other))}
    assert {name: (row.status, row.given_up_at is not None) for name, row in rows.items()} == {
        "held": ("cancelled", True),
        "taken": ("dispatched", True),
        "other": ("pending", False),
    }


# ── Revocation cuts the machine off ─────────────────────────────────


@pytest.mark.parametrize(("active", "handed"), [(True, True), (False, False)])
async def test_a_revoked_runner_is_handed_nothing(maker, presence, active, handed):
    runner_id = await _runner(
        maker, status="online" if active else "revoked", seen_ago=5, active=active
    )
    await _command(maker, runner_id, "pending")
    async with maker() as db:
        claimed = await runner_transport.claim_next_command(db, runner_id)
    assert (claimed is not None) is handed


@pytest.mark.parametrize(("active", "expected"), [(True, "pending"), (False, "revoked")])
async def test_a_command_is_never_queued_for_a_revoked_runner(
    maker, presence, terminal, active, expected
):
    """A tool or flow that loaded the runner before the revoke: dispatch reads
    it again under its row lock, so nothing is left queued that no transport
    will deliver and no revoke will end."""
    runner_id = await _runner(
        maker, status="online" if active else "revoked", seen_ago=5, active=active
    )
    command_id, status = await runner_transport.dispatch_command(
        runner_id=runner_id, kind="run_shell", payload={}, user_id=None
    )
    assert status == expected
    async with maker() as db:
        queued = await db.scalar(
            select(func.count()).select_from(RunnerCommand).where(RunnerCommand.runner_id == runner_id)
        )
    assert (bool(command_id), queued) == ((True, 1) if active else (False, 0))


async def test_a_dispatch_cancelled_after_its_commit_gives_the_command_up(
    maker, presence, terminal, monkeypatch
):
    """The caller never learns the id of a command committed just before it was
    cancelled, so nobody would wait for it: dispatch gives it up itself."""
    runner_id = await _runner(maker, status="online", seen_ago=5)
    monkeypatch.setattr(
        runner_transport, "_notify_new_command", AsyncMock(side_effect=asyncio.CancelledError)
    )
    with pytest.raises(asyncio.CancelledError):
        await runner_transport.dispatch_command(
            runner_id=runner_id, kind="run_shell", payload={}, user_id=None
        )
    async with maker() as db:
        row = (
            await db.execute(select(RunnerCommand).where(RunnerCommand.runner_id == runner_id))
        ).scalar_one()
    assert (row.status, row.given_up_at is not None) == ("cancelled", True)


async def test_a_decision_takes_the_runner_lock_before_the_command(
    maker, presence, terminal, monkeypatch
):
    """Runner then command, the revoke's order: while a revoke holds the runner
    row, a remembered approval waits before touching the command, so the two
    can never deadlock."""
    from app.api.v1 import runners as runners_api

    runner_id = await _runner(maker, status="online", seen_ago=5)
    async with maker() as db:
        cmd = RunnerCommand(
            runner_id=runner_id,
            kind="run_shell",
            payload={"command": "ls"},
            status="pending_approval",
        )
        db.add(cmd)
        await db.commit()
        command_id = cmd.id
    monkeypatch.setattr(runners_api, "log_audit", AsyncMock())
    monkeypatch.setattr(runners_api, "can_manage", lambda *_a, **_k: True)
    monkeypatch.setattr(runners_api, "authorize_command", lambda *_a, **_k: None)
    monkeypatch.setattr(runner_transport, "_notify_new_command", AsyncMock())

    async def visible(_runner_id, _user, db):
        return await db.get(Runner, runner_id), []

    monkeypatch.setattr(runners_api, "_get_visible_runner", visible)
    async with maker() as revoke_tx:
        await revoke_tx.execute(select(Runner.id).where(Runner.id == runner_id).with_for_update())
        async with maker() as db:
            approval = asyncio.create_task(
                runners_api.approve_command(
                    str(command_id),
                    MagicMock(),
                    runners_api.ApprovalDecision(remember=True),
                    MagicMock(),
                    db,
                )
            )
            async with maker() as probe:
                for _ in range(100):
                    waiting = await probe.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                        )
                    )
                    await probe.rollback()
                    if waiting:
                        break
                    await asyncio.sleep(0.05)
                assert waiting, "the approval never waited on the runner row"
                # The command row is still free: the approval waits on the runner.
                locked = await probe.scalar(
                    select(RunnerCommand.id)
                    .where(RunnerCommand.id == command_id)
                    .with_for_update(nowait=True)
                )
                assert locked == command_id
                await probe.rollback()
            await revoke_tx.rollback()
            outcome = await approval
    assert outcome["status"] == "pending"


def _socket(receive) -> MagicMock:
    websocket = MagicMock()
    websocket.receive_json = AsyncMock(side_effect=receive)
    websocket.close = AsyncMock()
    return websocket


async def test_a_revoked_runners_socket_closes_at_its_next_heartbeat(
    maker, presence, monkeypatch
):
    """The Redis revoke signal never arrives: the heartbeat's database read
    closes the socket anyway."""
    runner_id = await _runner(maker, status="online", seen_ago=5)
    monkeypatch.setattr(runner_transport, "_wake_listener", AsyncMock())
    monkeypatch.setattr(runner_transport, "_drain_and_push", AsyncMock(return_value=0))
    frames = 0

    async def receive():
        nonlocal frames
        frames += 1
        if frames > 1:
            raise WebSocketDisconnect()
        async with maker() as db:
            runner = await db.get(Runner, runner_id)
            runner.is_active = False
            runner.status = "revoked"
            await db.commit()
        return {"type": "heartbeat"}

    websocket = _socket(receive)
    await runner_transport.serve_runner_websocket(websocket, SimpleNamespace(id=runner_id))
    websocket.close.assert_awaited_once_with(code=4401)
    assert frames == 1
    assert await _status(maker, runner_id) == "revoked"


async def test_a_socket_revoked_before_it_was_served_is_closed_at_once(
    maker, presence, monkeypatch
):
    runner_id = await _runner(maker, status="revoked", seen_ago=5, active=False)
    drain = AsyncMock(return_value=0)
    monkeypatch.setattr(runner_transport, "_wake_listener", AsyncMock())
    monkeypatch.setattr(runner_transport, "_drain_and_push", drain)
    websocket = _socket(WebSocketDisconnect())
    await runner_transport.serve_runner_websocket(websocket, SimpleNamespace(id=runner_id))
    websocket.close.assert_awaited_once_with(code=4401)
    drain.assert_not_awaited()
    assert not runner_transport.has_local_connection(runner_id)


async def test_revoking_a_runner_ends_its_outstanding_commands(
    maker, presence, revoke, terminal
):
    runner_id = await _runner(maker, status="online", seen_ago=5)
    commands = {
        status: await _command(
            maker, runner_id, status, dispatched_ago=5 if status == "dispatched" else None
        )
        for status in ("pending", "pending_approval", "dispatched", "completed")
    }
    async with maker() as db:
        await revoke(db, await db.get(Runner, runner_id))
    after = {
        status: (await _command_row(maker, command_id)).status
        for status, command_id in commands.items()
    }
    assert after == {
        "pending": "cancelled",
        "pending_approval": "cancelled",
        "dispatched": "cancelled",
        "completed": "completed",
    }
    assert sorted(status for _, status in terminal) == ["cancelled"] * 3
