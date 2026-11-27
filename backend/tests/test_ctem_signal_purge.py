# SPDX-License-Identifier: Apache-2.0

"""#2528: the purge of every CTEM signal: admin gate, intake pause, job order, resume after a failure."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from app.api.deps import get_current_user
from app.api.v1 import ctem_signals as api_module
from app.core.config import settings
from app.core.database import get_db
from app.services import ctem_signal_purge as purge
from app.services import ctem_signals

ADMIN = "00000000-0000-4000-8000-000000000001"
OPERATOR = "00000000-0000-4000-8000-000000000002"


class _Redis:
    """A Redis enough for the purge: set (nx, ex), get, exists, delete."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def exists(self, key: str) -> int:
        return int(key in self.data)

    async def delete(self, key: str) -> int:
        return int(self.data.pop(key, None) is not None)


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> _Redis:
    fake = _Redis()

    async def _client() -> _Redis:
        return fake

    monkeypatch.setattr(purge, "_redis", _client)
    monkeypatch.setitem(purge._LOCAL, "paused", False)
    monkeypatch.setitem(purge._LOCAL, "status", None)
    return fake


# ── the intake pause ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_paused_intake_records_nothing_from_any_source(redis: _Redis) -> None:
    await purge.pause_intake()
    assert await purge.intake_paused()
    signal = ctem_signals.SignalInput(
        trigger_path="new_exposure", source_provider="manual", subject_kind="asset", subject_ref="a.example"
    )
    for via in ("api", "tool", "listener"):
        with pytest.raises(ctem_signals.SignalIntakePausedError):
            # Refused before any write: the session is never touched.
            await ctem_signals.record_signal(
                None,  # type: ignore[arg-type]
                organization_id=uuid.uuid4(),
                signal=signal,
                recorded_via=via,
                recorded_by="system",
            )
    await purge.resume_intake()
    assert not await purge.intake_paused() and redis.data == {}


@pytest.mark.asyncio
async def test_another_pod_sees_the_pause_through_redis(redis: _Redis) -> None:
    redis.data[purge.PAUSE_KEY] = "1"
    assert await purge.intake_paused()


@pytest.mark.asyncio
async def test_the_pause_fails_open_when_redis_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _down() -> Any:
        raise ConnectionError("no redis")

    monkeypatch.setattr(purge, "_redis", _down)
    monkeypatch.setitem(purge._LOCAL, "paused", False)
    assert not await purge.intake_paused()


@pytest.mark.asyncio
async def test_the_resync_step_records_through_the_pause(redis: _Redis) -> None:
    await purge.pause_intake()
    token = purge.RESYNC_BYPASS.set(True)
    try:
        assert purge.RESYNC_BYPASS.get() is True
    finally:
        purge.RESYNC_BYPASS.reset(token)
    assert purge.RESYNC_BYPASS.get() is False
    await purge.resume_intake()


# ── the job ─────────────────────────────────────────────────────────────


def _patch_steps(monkeypatch: pytest.MonkeyPatch, calls: list[str], *, fail: str = "") -> None:
    async def _cases(sessions: Any, actor_id: Any, *, progress: Any) -> int:
        calls.append(f"cases paused={purge._LOCAL['paused']}")
        if fail == "cases":
            raise RuntimeError("database gone")
        await progress(3)
        return 3

    async def _signals(sessions: Any, *, progress: Any) -> int:
        calls.append(f"signals paused={purge._LOCAL['paused']}")
        await progress(4400)
        return 4400

    async def _raised(db: Any) -> dict[str, int]:
        calls.append("raised")
        return {"surface_hosts": 2}

    async def _audit(sessions: Any, actor_id: Any, status: dict[str, Any]) -> None:
        calls.append(f"audit {status['state']}")

    async def _announce(status: dict[str, Any]) -> None:
        calls.append("announce")

    monkeypatch.setattr(purge, "purge_cases", _cases)
    monkeypatch.setattr(purge, "purge_signals", _signals)
    monkeypatch.setattr(purge, "clear_raised_state", _raised)
    monkeypatch.setattr(purge, "_audit", _audit)
    monkeypatch.setattr(purge, "_announce", _announce)


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def commit(self) -> None:
        return None


def _sessions() -> _Session:
    return _Session()


@pytest.mark.asyncio
async def test_the_job_runs_pause_purge_resume_in_order(redis: _Redis, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _patch_steps(monkeypatch, calls)
    status = purge.new_status("j1", SimpleNamespace(id=uuid.UUID(ADMIN), email="a@x"), resync=False)
    done = await purge.run_job(status, uuid.UUID(ADMIN), sessions=_sessions)
    assert calls == ["cases paused=True", "signals paused=True", "raised", "audit done", "announce"]
    assert done["state"] == "done" and done["error"] is None
    assert done["steps"]["purge"] == {"state": "done", "counts": {"cases": 3, "signals": 4400, "surface_hosts": 2}}
    assert done["steps"]["resync"]["state"] == "skipped" and done["steps"]["resume"]["state"] == "done"
    assert not await purge.intake_paused()


@pytest.mark.asyncio
async def test_intake_resumes_after_a_forced_failure(redis: _Redis, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _patch_steps(monkeypatch, calls, fail="cases")
    status = purge.new_status("j2", SimpleNamespace(id=uuid.UUID(ADMIN)), resync=True)
    done = await purge.run_job(status, uuid.UUID(ADMIN), sessions=_sessions)
    assert done["state"] == "failed" and done["error"].startswith("purge: ")
    assert done["steps"]["purge"]["state"] == "failed" and done["steps"]["resync"]["state"] == "pending"
    assert done["steps"]["resume"]["state"] == "done"
    assert not await purge.intake_paused() and purge.PAUSE_KEY not in redis.data
    assert "audit failed" in calls and purge.JOB_LOCK_KEY not in redis.data


@pytest.mark.asyncio
async def test_a_running_status_whose_job_lock_expired_reads_back_interrupted(redis: _Redis) -> None:
    """#2539: the pod restarted mid-job, the lock expired, the status was left running."""
    status = purge.new_status("j9", SimpleNamespace(id=uuid.UUID(ADMIN)), resync=False)
    status["step"] = "purge"
    status["steps"]["pause"]["state"] = "done"
    status["steps"]["purge"] = {"state": "running", "counts": {"cases": 12}}
    await purge._write_status(status)
    assert purge.JOB_LOCK_KEY not in redis.data

    read = await purge.read_status()
    assert read is not None and read["state"] == "failed" and read["interrupted"] is True
    assert read["error"] == purge.INTERRUPTED_ERROR
    assert read["steps"]["purge"] == {"state": "failed", "counts": {"cases": 12}}
    assert read["steps"]["pause"]["state"] == "done"
    # The intake pause expired with the lock: intake runs again, so the resume step is true as done.
    assert purge.PAUSE_KEY not in redis.data
    assert read["intake_paused"] is False
    assert read["steps"]["resume"]["state"] == "done"


@pytest.mark.asyncio
async def test_an_interrupted_job_without_the_pause_skips_its_pending_resync(redis: _Redis) -> None:
    """#2539 UX B1: a resync that never started is skipped once intake runs again."""
    status = purge.new_status("j11", SimpleNamespace(id=uuid.UUID(ADMIN)), resync=True)
    status["step"] = "purge"
    status["steps"]["pause"]["state"] = "done"
    status["steps"]["purge"]["state"] = "running"
    await purge._write_status(status)

    read = await purge.read_status()
    assert read is not None and read["interrupted"] is True and read["intake_paused"] is False
    assert read["steps"]["resync"]["state"] == "skipped"
    assert read["steps"]["resume"]["state"] == "done"


@pytest.mark.asyncio
async def test_an_interrupted_job_whose_pause_is_still_there_keeps_resume_pending(redis: _Redis) -> None:
    """#2539 UX B1: the pause key outlives the lock: intake is still paused and the status says so."""
    status = purge.new_status("j12", SimpleNamespace(id=uuid.UUID(ADMIN)), resync=True)
    status["step"] = "purge"
    status["steps"]["pause"]["state"] = "done"
    status["steps"]["purge"]["state"] = "running"
    await purge._write_status(status)
    redis.data[purge.PAUSE_KEY] = "1"

    read = await purge.read_status()
    assert read is not None and read["interrupted"] is True and read["intake_paused"] is True
    assert read["steps"]["resume"]["state"] == "pending"
    assert read["steps"]["resync"]["state"] == "pending"
    assert read["steps"]["purge"]["state"] == "failed"


@pytest.mark.asyncio
async def test_a_running_status_with_its_job_lock_stays_running(redis: _Redis) -> None:
    status = purge.new_status("j10", SimpleNamespace(id=uuid.UUID(ADMIN)), resync=False)
    await purge._write_status(status)
    redis.data[purge.JOB_LOCK_KEY] = "1"
    read = await purge.read_status()
    assert read is not None and read["state"] == "running" and "interrupted" not in read


@pytest.mark.asyncio
async def test_a_failed_resync_still_resumes_intake(redis: _Redis, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _patch_steps(monkeypatch, calls)

    async def _resync(**kw: Any) -> dict[str, Any]:
        assert purge.RESYNC_BYPASS.get() is True and await purge.intake_paused()
        await kw["progress"]("new_vulnerability", 12)
        raise RuntimeError("OpenCTI answered 502")

    status = purge.new_status("j3", SimpleNamespace(id=uuid.UUID(ADMIN)), resync=True)
    done = await purge.run_job(status, uuid.UUID(ADMIN), sessions=_sessions, resync=_resync)
    assert done["state"] == "failed" and done["error"] == "resync: OpenCTI answered 502"
    assert done["steps"]["resync"]["counts"]["paths"] == {"new_vulnerability": 12}
    assert not await purge.intake_paused() and purge.RESYNC_BYPASS.get() is False


@pytest.mark.asyncio
async def test_one_job_at_a_time(redis: _Redis) -> None:
    assert await purge._take_job() is True
    assert await purge._take_job() is False
    await purge._release_job()
    assert await purge._take_job() is True


# ── the API ─────────────────────────────────────────────────────────────


@pytest.fixture
def client(redis: _Redis, monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)
    app = FastAPI()
    app.include_router(api_module.router, prefix="/api/v1/ctem-signals")

    async def _db():
        yield SimpleNamespace()

    async def _user(request: Request):
        uid = request.headers.get("X-User", ADMIN)
        return SimpleNamespace(id=uuid.UUID(uid), is_admin=uid == ADMIN, email=f"{uid}@test")

    async def _right(_db: Any, user_id: Any) -> str | None:
        return "decide" if str(user_id) == OPERATOR else None

    async def _counts(_db: Any) -> dict[str, int]:
        return {"signals": 4400, "cases": 37}

    started: list[bool] = []

    async def _start(actor: Any, *, resync: bool, windows: Any = None) -> dict[str, Any]:
        if started:
            raise purge.PurgeBusyError(purge.BUSY_DETAIL)
        started.append(resync)
        return purge.new_status("job", actor, resync=resync, windows=windows)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user
    monkeypatch.setattr("app.services.ctem_access.group_right", _right)
    monkeypatch.setattr(purge, "counts", _counts)
    monkeypatch.setattr(purge, "start", _start)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_a_non_admin_gets_403_on_both_routes(client: httpx.AsyncClient) -> None:
    async with client as http:
        read = await http.get("/api/v1/ctem-signals/purge", headers={"X-User": OPERATOR})
        start = await http.post("/api/v1/ctem-signals/purge", json={"confirm": "PURGE"}, headers={"X-User": OPERATOR})
    assert read.status_code == 403 and start.status_code == 403


@pytest.mark.asyncio
async def test_an_admin_reads_the_live_counts(client: httpx.AsyncClient) -> None:
    async with client as http:
        answer = await http.get("/api/v1/ctem-signals/purge")
    assert answer.status_code == 200
    body = answer.json()
    assert {k: body[k] for k in ("counts", "confirm_word", "job")} == {
        "counts": {"signals": 4400, "cases": 37},
        "confirm_word": "PURGE",
        "job": None,
    }
    # #2578: the dialog draws one row per resync route with its default window.
    routes = {r["key"]: r for r in body["resync_routes"]}
    assert routes["vulnerability"]["default"] == 7 and routes["intrusion_set"]["default"] == 30
    assert routes["aikido"]["default"] is None and routes["attack_surface"]["windowed"] is False
    assert body["resync_max_days"] == 365


@pytest.mark.asyncio
async def test_the_purge_needs_the_typed_word_and_runs_one_job(client: httpx.AsyncClient) -> None:
    async with client as http:
        wrong = await http.post("/api/v1/ctem-signals/purge", json={"confirm": "purge please"})
        first = await http.post("/api/v1/ctem-signals/purge", json={"confirm": "PURGE"})
        second = await http.post("/api/v1/ctem-signals/purge", json={"confirm": "PURGE", "resync": True})
    assert wrong.status_code == 422
    assert first.status_code == 202 and first.json()["job"]["resync"] is False
    assert first.json()["job"]["steps"]["resync"]["state"] == "skipped"
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_the_resync_windows_are_validated_and_carried_by_the_job(client: httpx.AsyncClient) -> None:
    """#2578: a bad window is a 422 before any job; a good one lands on the job with the defaults filled in."""
    async with client as http:
        bad = await http.post(
            "/api/v1/ctem-signals/purge",
            json={"confirm": "PURGE", "resync": True, "resync_windows": {"vulnerability": 0}},
        )
        unknown = await http.post(
            "/api/v1/ctem-signals/purge", json={"confirm": "PURGE", "resync": True, "resync_windows": {"x": 3}}
        )
        good = await http.post(
            "/api/v1/ctem-signals/purge",
            json={"confirm": "PURGE", "resync": True, "resync_windows": {"vulnerability": 3, "aikido": 90}},
        )
    assert bad.status_code == 422 and unknown.status_code == 422
    windows = good.json()["job"]["windows"]
    assert windows["vulnerability"] == 3 and windows["aikido"] == 90
    assert windows["malware"] == 30 and windows["github"] is None
