"""The runner result upload against PostgreSQL.

``test_runner_result_upload.py`` drives the route through a scripted session;
these run the same route, its row lock and ``runner_transport.submit_result``
on a real database: a refused result ends the command as ``failed`` once, the
result a redelivery brings afterwards changes nothing, and the command
history can be read without loading a single result.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.deps import get_current_runner
from app.api.v1 import runners as runners_api
from app.models.runner import Runner, RunnerCommand
from app.services import runner_transport
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

BOUNDARY = "xtm-one-runner-test-boundary"


@pytest.fixture
async def sessions(monkeypatch):
    monkeypatch.setattr(runners_api.settings, "runners_enabled", True)
    monkeypatch.setattr(runner_transport, "_notify_flow_engine", AsyncMock())
    async with scratch_database("result_upload", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _dispatched_command(sessions, **columns) -> tuple[Runner, uuid.UUID]:
    async with sessions() as db:
        runner = Runner(
            name="desk", platform="linux", key_prefix="run-test", key_hash="hash", is_active=True
        )
        db.add(runner)
        await db.flush()
        command = RunnerCommand(
            runner_id=runner.id,
            kind="run_shell",
            payload={"command": "cat big.log"},
            dispatched_at=datetime.now(UTC),
            **{"status": "dispatched", **columns},
        )
        db.add(command)
        await db.commit()
        return runner, command.id


def _upload_body(meta: str) -> bytes:
    return (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="meta"\r\n\r\n{meta}\r\n'
        f"--{BOUNDARY}--\r\n"
    ).encode()


async def _post(runner: Runner, command_id: uuid.UUID, meta: str) -> httpx.Response:
    app = FastAPI()
    app.include_router(runners_api.router, prefix="/api/v1/runners")
    app.dependency_overrides[get_current_runner] = lambda: runner
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://platform") as client:
        return await client.post(
            f"/api/v1/runners/commands/{command_id}/result",
            content=_upload_body(meta),
            headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
        )


async def test_a_refused_result_fails_the_command_once(sessions, monkeypatch):
    monkeypatch.setattr(runner_transport, "RUNNER_MAX_RESULT_META_BYTES", 4096)
    runner, command_id = await _dispatched_command(sessions)
    too_large = json.dumps({"status": "completed", "result": {"stdout": "x" * 8192}})

    refused = await _post(runner, command_id, too_large)

    assert refused.status_code == 400, refused.text
    async with sessions() as db:
        command = await db.get(RunnerCommand, command_id)
    assert command.status == "failed"
    assert command.result is None
    assert command.error.startswith("The platform refused this command's result: ")
    failed_at = command.completed_at
    assert failed_at is not None

    # The same command delivered again after its lease: that result is dropped.
    again = await _post(runner, command_id, json.dumps({"status": "completed", "result": {}}))

    assert again.status_code == 200 and again.json() == {"ok": True}
    async with sessions() as db:
        command = await db.get(RunnerCommand, command_id)
    assert (command.status, command.completed_at) == ("failed", failed_at)


async def test_the_history_is_read_without_its_results(sessions, monkeypatch):
    runner, command_id = await _dispatched_command(
        sessions, result={"stdout": "x" * 65536}, status="completed"
    )

    async def visible(*_args, **_kwargs):
        return runner, []

    monkeypatch.setattr(runners_api, "_get_visible_runner", visible)
    monkeypatch.setattr(runners_api, "can_manage", lambda *_a, **_k: True)
    monkeypatch.setattr(runners_api, "is_admin_oversight_read", lambda *_a, **_k: False)
    owner = SimpleNamespace(id=uuid.uuid4(), is_admin=False, role="user")
    request = SimpleNamespace(headers={}, client=None)

    async def read(include_result: bool) -> list[dict]:
        async with sessions() as db:
            return await runners_api.list_runner_commands(
                str(runner.id), request, limit=20, include_result=include_result, user=owner, db=db
            )

    without = await read(False)
    assert [(row["id"], row["status"], row["result"]) for row in without] == [
        (str(command_id), "completed", None)
    ]
    [served] = await read(True)
    assert served["result"] == {"stdout": "x" * 65536}
