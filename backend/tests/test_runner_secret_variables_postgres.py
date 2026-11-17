"""A Desktop runner command with a secret variable in it, on a real PostgreSQL.

The agent's tool call holds the value. The stored command, its audit entry and
the command history hold none of it; the runner is handed the value when the
command is delivered, outside the turn that dispatched it; and what the
machine sends back is stored with ``[secret]`` in its place.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.redaction import SECRET_MARKER_RE, current_known_secrets, known_secrets_scope, remember_secret_value
from app.core.security import encrypt_value
from app.models.audit_log import AuditLog
from app.models.runner import PERMISSION_MODE_FULL_CONTROL, Runner, RunnerCommand
from app.models.user import User
from app.models.variable import Variable
from app.services import runner_transport
from app.services.runners import serialize_command
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

SECRET = "ghp_RunnerSecretValue1234567890"
COMMAND = f"curl -sH 'Authorization: Bearer {SECRET}' https://api.github.com/user"


@pytest.fixture
async def sessions(monkeypatch):
    monkeypatch.setattr(runner_transport, "_notify_new_command", AsyncMock())
    monkeypatch.setattr(runner_transport, "_announce_command_terminal", AsyncMock())
    monkeypatch.setattr(runner_transport, "_emit_command_terminal", AsyncMock())
    async with scratch_database("runner_secrets", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _seed(sessions) -> tuple[User, Variable, Runner]:
    owner = User(
        id=uuid.uuid4(),
        email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name="Owner",
        role="user",
    )
    variable = Variable(
        id=uuid.uuid4(),
        user_id=owner.id,
        is_company_managed=False,
        name="github_token",
        is_secret=True,
        value_enc=encrypt_value(SECRET),
    )
    runner = Runner(
        name="desk",
        platform="linux",
        key_prefix="run-test",
        key_hash="hash",
        is_active=True,
        user_id=owner.id,
        permission_mode=PERMISSION_MODE_FULL_CONTROL,
    )
    async with sessions() as db:
        db.add(owner)
        await db.flush()
        db.add_all([variable, runner])
        await db.commit()
    return owner, variable, runner


async def _dispatch(owner: User, variable: Variable, runner: Runner) -> uuid.UUID:
    conversation_id = uuid.uuid4()
    with known_secrets_scope(conversation_id):
        remember_secret_value(variable.id, SECRET)
        command_id, status = await runner_transport.dispatch_command(
            runner_id=runner.id,
            kind="run_shell",
            payload={"command": COMMAND},
            user_id=owner.id,
            conversation_id=conversation_id,
        )
    assert status == "pending"
    return uuid.UUID(command_id)


async def _deliver(sessions, runner: Runner) -> dict:
    # The fast path drains inside whichever turn woke the runner: here, another user's.
    with known_secrets_scope(uuid.uuid4()) as other_turn:
        async with sessions() as db:
            delivered = await runner_transport.claim_next_command(db, runner.id)
        assert not other_turn
    assert current_known_secrets() is None
    return delivered


async def test_a_runner_command_holds_no_secret_and_runs_with_it(sessions):
    owner, variable, runner = await _seed(sessions)
    command_id = await _dispatch(owner, variable, runner)

    async with sessions() as db:
        row = await db.get(RunnerCommand, command_id)
        audit = (await db.execute(select(AuditLog).where(AuditLog.action == "runner_command"))).scalar_one()
    assert SECRET not in json.dumps(row.payload)
    assert SECRET_MARKER_RE.search(row.payload["command"])
    assert SECRET not in json.dumps(audit.details)
    assert serialize_command(row)["payload"] == {"command": COMMAND.replace(SECRET, "[secret]")}

    delivered = await _deliver(sessions, runner)
    assert delivered["id"] == str(command_id)
    assert delivered["payload"]["command"] == COMMAND

    assert await runner_transport.submit_result(
        command_id,
        status="completed",
        result={"stdout": f'{{"token": "{SECRET}"}}', "exit_code": 0},
        error=f"warning: {SECRET} was printed",
    )
    async with sessions() as db:
        row = await db.get(RunnerCommand, command_id)
    assert row.result == {"stdout": '{"token": "[secret]"}', "exit_code": 0}
    assert row.error == "warning: [secret] was printed"


async def test_a_result_after_the_value_was_rotated_is_not_kept(sessions):
    owner, variable, runner = await _seed(sessions)
    command_id = await _dispatch(owner, variable, runner)
    assert (await _deliver(sessions, runner))["payload"]["command"] == COMMAND

    async with sessions() as db:
        row = await db.get(RunnerCommand, command_id)
        assert SECRET not in json.dumps(row.payload)
        assert serialize_command(row)["payload"] == {"command": COMMAND.replace(SECRET, "[secret]")}
        stored = await db.get(Variable, variable.id)
        stored.value_enc = encrypt_value("ghp_RotatedSecretValue0987654321")
        await db.commit()

    assert await runner_transport.submit_result(
        command_id, status="completed", result={"stdout": SECRET, "exit_code": 0}, error=f"echoed {SECRET}"
    )
    async with sessions() as db:
        row = await db.get(RunnerCommand, command_id)
    assert (row.status, row.result) == ("failed", None)
    assert SECRET not in row.error


async def test_a_variable_gone_before_delivery_stays_a_marker(sessions):
    owner, variable, runner = await _seed(sessions)
    await _dispatch(owner, variable, runner)
    async with sessions() as db:
        await db.execute(delete(Variable).where(Variable.id == variable.id))
        await db.commit()

    delivered = await _deliver(sessions, runner)

    assert SECRET not in delivered["payload"]["command"]
    assert SECRET_MARKER_RE.search(delivered["payload"]["command"])
