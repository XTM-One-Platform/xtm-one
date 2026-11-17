"""Shared doubles for the inbound (server-side) A2A test suite.

The mirror of ``a2a_client_stubs`` for the direction this platform serves. Four
objects every file on this surface needs live here — a result with a
``rowcount``, an ordered fake session, an async-context wrapper for
``async_session``, and the task/run/exposure/agent quartet ``execute_task_run``
loads — so a change to the executor's load order lands in one place rather than
in every file that guessed it.

Deliberately doubles rather than a live server with a real database: these tests
assert what the executor *decides* — which state a task reports, which agent
answered, whether a cancel reached the children — and none of those decisions
becomes more true against a socket. Where the database itself is the subject (a
migration backfill, an index actually being used), the test carries the
``integration`` marker and talks to real Postgres instead.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi.testclient import TestClient

#: A partner reaches this surface over https, and ``is_secure_transport`` reads
#: the client's scheme, so the suite's clients state one.
SECURE_BASE_URL = "https://testserver"


def partner_client(app) -> TestClient:
    """A client whose transport the surface can confirm."""
    return TestClient(app, base_url=SECURE_BASE_URL)


class Result:
    """One statement's answer. ``rowcount`` because the guarded UPDATEs read it."""

    def __init__(self, value: Any = None, rowcount: int = 1):
        self._value = value
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._value

    def one_or_none(self):
        # ``resolve_state`` reads the task again with its run; the task
        # is refreshed in place, so the row's run is the answer that matters.
        return None if self._value is None else (None, self._value)

    def scalars(self):
        return self

    def all(self):
        return self._value if isinstance(self._value, list) else []


class FakeDb:
    """Answers statements in the order given, then ``None`` with rowcount 1.

    The order is the executor's load order, so a test reads as a list of what
    the database holds rather than as a matcher on SQL.
    """

    def __init__(self, *answers: Any):
        self._answers = list(answers)
        self.commits = 0
        self.flushes = 0
        self.is_active = True

    async def execute(self, _stmt):
        answer = self._answers.pop(0) if self._answers else None
        return answer if isinstance(answer, Result) else Result(answer)

    async def commit(self):
        self.commits += 1

    async def flush(self):
        self.flushes += 1

    async def rollback(self):
        pass

    async def refresh(self, _obj):
        pass

    def add(self, _obj):
        pass


class Ctx:
    """``async_session()`` stand-in yielding one :class:`FakeDb`."""

    def __init__(self, db: FakeDb):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc):
        return False


class DedupRedis:
    """A Redis holding the messageId claims of one test, and nothing else.

    ``_claim_message_id`` takes ``a2a:dedup:<client>:<message>`` with ``SET NX``
    and ``_release_message_id`` deletes it. Any other command answers as an
    outage, which is what it meets in CI's unit job, where there is no Redis.
    """

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    async def delete(self, key):
        return 1 if self.keys.pop(key, None) is not None else 0

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        async def _outage(*_a, **_k):
            raise ConnectionError(f"no Redis in the unit suite ({name})")

        return _outage


def own_dedup_claims(monkeypatch) -> DedupRedis:
    """Point the dedup claim at a :class:`DedupRedis` the test owns.

    Left to the environment, the claim reaches the Redis ``REDIS_URL`` names
    and keeps its key for the whole dedup window. A developer's Redis then
    still holds a fixed id from an earlier run and refuses it as another
    task's, while CI's unit job, which has no Redis, grants it. Whether a claim
    reaches that Redis at all depends on the test order: the shared client's
    connection belongs to the event loop of the test that opened it, and a
    later test's claim fails open on it.
    """
    redis = DedupRedis()

    async def _client():
        return redis

    monkeypatch.setattr("app.core.redis.redis_client", _client)
    return redis


def agent(**overrides) -> Any:
    """An agent with no tools and no peers unless a test says otherwise."""
    fields = {
        "id": uuid.uuid4(),
        "name": "Exposed Agent",
        "description": "",
        # Read by the task route before anything else: a disabled agent is
        # not found, so a stub without this is invisible to the surface.
        "enabled": True,
        "builtin_tool_ids": [],
        "custom_tool_ids": [],
        "mcp_server_ids": [],
        "knowledge_base_ids": [],
        "handover_agent_ids": [],
        "handover_types": {},
        "output_format": "text",
        "integration_bindings": {},
        "integration_template_bindings": [],
    }
    fields.update(overrides)
    return type("Agent", (), fields)()


def exposure(**overrides) -> Any:
    fields = {
        "id": uuid.uuid4(),
        "revoked_at": None,
        "run_as_user_id": uuid.uuid4(),
        "card_token": "tok-test",
    }
    fields.update(overrides)
    return type("Exposure", (), fields)()


def run(agent_id: uuid.UUID | None = None, **overrides) -> Any:
    fields = {
        "id": uuid.uuid4(),
        "status": "pending",
        "agent_id": agent_id or uuid.uuid4(),
        "result_summary": None,
        "error": None,
        "completed_at": None,
        "trigger_context": {"client_id": "partner-1"},
        "trigger_payload": {"message": "do the thing"},
    }
    fields.update(overrides)
    return type("Run", (), fields)()


def task(run_id: uuid.UUID | None = None, **overrides) -> Any:
    from datetime import UTC, datetime

    fields = {
        "id": uuid.uuid4(),
        # Recent by default: the reaper reads it for the total-lifetime bound,
        # and a test about anything else must not trip that bound by accident.
        "created_at": datetime.now(UTC),
        "context_id": uuid.uuid4(),
        "run_id": run_id,
        "exposure_id": uuid.uuid4(),
        "client_id": "partner-1",
        "message_id": None,
        "rejected_reason": None,
        "cancel_requested_at": None,
        "lease_heartbeat_at": None,
        "reaped": False,
        "responding_agent_id": None,
        "caller_question": None,
        "clarification_count": 0,
    }
    fields.update(overrides)
    return type("Task", (), fields)()


def drive_executor(
    monkeypatch,
    *,
    returns: tuple[str, str | None] = ("answer", None),
    error_out: dict | None = None,
    live_children: bool = False,
    agent_kwargs: dict | None = None,
) -> dict:
    """Wire ``execute_task_run`` to a scripted chat outcome.

    Returns the dict ``_finalise_run`` was called with — the terminal decision
    under test — plus the doubles under ``_task``, ``_run``, ``_exposure`` and
    ``_agent`` for a test that needs to assert against them.
    """
    from app.a2a import tasks as task_svc

    _agent = agent(**(agent_kwargs or {}))
    _run = run(_agent.id)
    _task = task(_run.id, exposure_id=uuid.uuid4())
    _exposure = exposure(id=_task.exposure_id)
    captured: dict = {
        "_task": _task,
        "_run": _run,
        "_exposure": _exposure,
        "_agent": _agent,
    }

    async def _fake_chat(**kwargs):
        if error_out is not None:
            kwargs["error_out"].update(error_out)
        captured["chat_kwargs"] = kwargs
        return returns

    async def _fake_finalise(_db, _t, _rid, status, summary, error, **kw):
        captured.update(status=status, summary=summary, error=error, **kw)

    async def _noop(*_a, **_k):
        return None

    async def _children(*_a, **_k):
        return live_children

    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _fake_chat)
    monkeypatch.setattr(task_svc, "_supervise_run", _noop)
    monkeypatch.setattr(task_svc, "_finalise_run", _fake_finalise)
    if hasattr(task_svc, "live_delegated_work"):
        monkeypatch.setattr(task_svc, "live_delegated_work", _children)
    monkeypatch.setattr(
        "app.core.database.async_session",
        lambda: Ctx(FakeDb(_task, _run, _exposure, _agent)),
    )
    return captured


def silence_delegated_work(monkeypatch, task_svc) -> None:
    """Answer the two delegated-work queries with "none", for a file whose
    subject is the projection itself rather than the work a run delegated.

    Without it every double in such a file has to answer ``resolve_state`` and
    ``build_artifacts`` asking after children it never had.
    """

    async def _none(*_a, **_k):
        return False

    async def _empty(*_a, **_k):
        return []

    monkeypatch.setattr(task_svc, "live_delegated_work", _none)
    monkeypatch.setattr(task_svc, "_delegated_artifacts", _empty)
