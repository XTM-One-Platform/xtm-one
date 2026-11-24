"""The audited oversight read of a machine's command history.

``GET /runners/{id}/commands`` gives a manager the WHOLE history of a machine
- shell payloads and output, inline file contents - and an admin manages every
machine through the by-id bypass ``_get_visible_runner`` keeps. The
admin Objects browser now opens every enrolled machine on ``/desktop/runners/:id``, so
a colleague's PRIVATE laptop is one click away for an administrator; the read
therefore leaves the same kind of trail the break-glass read of another
user's conversation files does: one audit entry per admin, per machine, per
window, written before the history is read.

These tests drive the real endpoint with a scripted session and a fake Redis,
so the predicate (WHO is an oversight reader), the throttle, the fail-open
Redis path and the commit ordering are each pinned.
"""

from __future__ import annotations

import re
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import runners as runners_api
from app.api.v1.helpers import SHARE_PERMISSION_CONFIG, SHARE_PERMISSION_READ_ONLY
from app.models.audit_log import AuditLog
from app.models.runner import Runner

OWNER_EMAIL = "owner@example.com"


def _user(*, admin: bool) -> SimpleNamespace:
    uid = uuid.uuid4()
    return SimpleNamespace(
        id=uid,
        role="admin" if admin else "user",
        is_admin=admin,
        email=f"{'admin' if admin else 'member'}-{uid.hex[:6]}@example.com",
    )


def _runner(
    *,
    owner: uuid.UUID | None,
    company_managed: bool = False,
    grants: dict[str, str] | None = None,
) -> Runner:
    """A machine as the ORM would load it (no database involved)."""
    return Runner(
        id=uuid.uuid4(),
        user_id=owner,
        name="dc01-laptop",
        platform="windows",
        key_prefix="run-abcdefgh...",
        key_hash="hash",
        is_active=True,
        is_company_managed=company_managed,
        shared_user_ids=list(grants) if grants else None,
        shared_user_permissions=dict(grants) if grants else None,
    )


class _Scalar:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _Session:
    """Answers each statement by the table it reads; records adds and commits."""

    def __init__(self, runner: Runner | None):
        self.runner = runner
        self.statements: list[str] = []
        self.added: list = []
        self.commits_at: list[int] = []
        self.history_rows: list = []

    async def execute(self, statement, *a, **k):
        sql = re.sub(r"\s+", " ", str(statement))
        self.statements.append(sql)
        if "FROM group_members" in sql:
            return _Rows([])
        if "FROM runner_commands" in sql:
            return _Rows(self.history_rows)
        if "FROM runners" in sql:
            return _Scalar(self.runner)
        if "users.email" in sql:
            return _Scalar(OWNER_EMAIL)
        raise AssertionError(f"unexpected statement: {sql}")

    def add(self, entry):
        self.added.append(entry)

    async def flush(self):
        return None

    async def commit(self):
        self.commits_at.append(len(self.statements))

    @property
    def history_sql(self) -> str:
        return next(s for s in self.statements if "FROM runner_commands" in s)

    @property
    def history_where(self) -> str:
        """The predicate of the history read - the SELECT list names every
        column, so only the WHERE tells a scoped read from the fleet audit."""
        sql = self.history_sql
        return sql.split(" WHERE ", 1)[1].split(" ORDER BY ", 1)[0]

    @property
    def audits(self) -> list[AuditLog]:
        return [e for e in self.added if isinstance(e, AuditLog)]


class _Redis:
    """The marker store: ``EXISTS`` answers from what ``SET`` wrote, or raises."""

    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.keys: dict[str, dict] = {}
        self.exists_calls: list[str] = []
        self.set_calls: list[tuple[str, dict]] = []

    async def exists(self, key):
        if self.error is not None:
            raise self.error
        self.exists_calls.append(key)
        return 1 if key in self.keys else 0

    async def set(self, key, value, **options):
        if self.error is not None:
            raise self.error
        self.set_calls.append((key, options))
        self.keys[key] = options
        return True


def _install_redis(monkeypatch, fake: _Redis) -> None:
    async def _client():
        return fake

    monkeypatch.setattr(runners_api, "redis_client", _client)


async def _read_history(user, session, *, request=None):
    return await runners_api.list_runner_commands(
        str(session.runner.id) if session.runner else str(uuid.uuid4()),
        request or SimpleNamespace(headers={}, client=None),
        limit=20,
        user=user,
        db=session,
    )


# ── The predicate ───────────────────────────────────────────────────────────


def test_only_the_admin_role_alone_makes_an_oversight_read():
    admin = _user(admin=True)
    member = _user(admin=False)

    # The bypass case: a colleague's private machine, no grant of any kind.
    assert runners_api.is_admin_oversight_read(admin, _runner(owner=member.id), [])
    # A read_only grant does not manage anything: the full history still comes
    # through the role, so it is still an oversight read.
    assert runners_api.is_admin_oversight_read(
        admin, _runner(owner=member.id, grants={str(admin.id): SHARE_PERMISSION_READ_ONLY}), []
    )

    # What the admin was GIVEN is not oversight.
    assert not runners_api.is_admin_oversight_read(admin, _runner(owner=admin.id), [])
    assert not runners_api.is_admin_oversight_read(
        admin, _runner(owner=member.id, grants={str(admin.id): SHARE_PERMISSION_CONFIG}), []
    )
    assert not runners_api.is_admin_oversight_read(
        admin, _runner(owner=None, company_managed=True), []
    )
    # A member never reads through a bypass - the visibility query scopes them.
    assert not runners_api.is_admin_oversight_read(member, _runner(owner=admin.id), [])


# ── The endpoint ────────────────────────────────────────────────────────────


async def test_an_admin_reading_a_colleagues_private_history_is_audited_once_per_window(monkeypatch):
    admin = _user(admin=True)
    member = _user(admin=False)
    session = _Session(_runner(owner=member.id))
    redis = _Redis()
    _install_redis(monkeypatch, redis)

    assert await _read_history(admin, session) == []

    # The history the admin gets is the WHOLE fleet audit, not their own rows.
    assert "runner_commands.user_id" not in session.history_where
    # One entry, naming the admin, the machine and its owner.
    (entry,) = session.audits
    assert entry.action == runners_api.RUNNER_HISTORY_OVERSIGHT_ACTION == "break_glass"
    assert entry.entity_type == runners_api.RUNNER_HISTORY_OVERSIGHT_ENTITY_TYPE == "runner_commands"
    assert entry.entity_id == session.runner.id
    assert entry.entity_name == "dc01-laptop"
    assert admin.email in entry.summary and OWNER_EMAIL in entry.summary
    assert entry.details["runner_owner_id"] == str(member.id)
    assert entry.details["runner_owner_email"] == OWNER_EMAIL
    assert entry.details["window_seconds"] == runners_api.RUNNER_HISTORY_OVERSIGHT_AUDIT_WINDOW_SECONDS
    # Committed BEFORE the history was read: the trail cannot be lost to a
    # failure of the read it accounts for.
    history_index = session.statements.index(session.history_sql)
    assert session.commits_at == [history_index]
    # The marker is the throttle: per admin, per machine, for one window,
    # written only once the row is committed (an unmarked window can cost a
    # duplicate row, a marker with no row behind it would hide a read).
    key = runners_api.runner_history_oversight_key(admin.id, session.runner.id)
    assert str(admin.id) in key and str(session.runner.id) in key
    assert redis.exists_calls == [key]
    assert redis.set_calls == [(key, {"ex": runners_api.RUNNER_HISTORY_OVERSIGHT_AUDIT_WINDOW_SECONDS})]

    # The card polls: the second read inside the window writes nothing more.
    await _read_history(admin, session)
    assert len(session.audits) == 1
    assert redis.exists_calls == [key, key] and len(redis.set_calls) == 1
    assert len(session.commits_at) == 1


async def test_a_failed_audit_write_leaves_no_marker_behind(monkeypatch):
    """A marker claimed before the row is durable would cover the retry that
    follows with no record behind it: the marker is written
    after the commit, so a failed audit fails the read, marks nothing, and the
    next read audits again."""
    admin = _user(admin=True)
    session = _Session(_runner(owner=uuid.uuid4()))
    redis = _Redis()
    _install_redis(monkeypatch, redis)

    failures = [RuntimeError("database gone")]
    healthy_commit = session.commit

    async def flaky_commit():
        if failures:
            raise failures.pop()
        await healthy_commit()

    session.commit = flaky_commit  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await _read_history(admin, session)
    assert redis.set_calls == [], "no marker without a committed row"
    assert not any("FROM runner_commands" in s for s in session.statements), "the read did not happen"

    await _read_history(admin, session)
    assert len(session.audits) == 2, "the retry is audited"
    assert len(redis.set_calls) == 1 and session.commits_at == [session.statements.index(session.history_sql)]


async def test_a_redis_outage_audits_anyway(monkeypatch):
    """A missing marker must never mean a missing record."""
    admin = _user(admin=True)
    session = _Session(_runner(owner=uuid.uuid4()))
    _install_redis(monkeypatch, _Redis(error=ConnectionError("redis down")))

    await _read_history(admin, session)

    assert len(session.audits) == 1
    assert session.commits_at, "the entry is committed even without the marker"


async def test_the_owner_and_the_granted_leave_no_trail(monkeypatch):
    redis = _Redis()
    _install_redis(monkeypatch, redis)

    # An admin on their OWN machine.
    admin = _user(admin=True)
    own = _Session(_runner(owner=admin.id))
    await _read_history(admin, own)
    assert own.audits == [] and "runner_commands.user_id" not in own.history_where

    # An admin managing the company fleet - that is the job, not a bypass.
    fleet = _Session(_runner(owner=None, company_managed=True))
    await _read_history(admin, fleet)
    assert fleet.audits == [] and "runner_commands.user_id" not in fleet.history_where

    # A member holding a config grant reads the full history they were given.
    member = _user(admin=False)
    granted = _Session(_runner(owner=uuid.uuid4(), grants={str(member.id): SHARE_PERMISSION_CONFIG}))
    await _read_history(member, granted)
    assert granted.audits == [] and "runner_commands.user_id" not in granted.history_where

    assert redis.exists_calls == [] and redis.set_calls == [], "no marker is even consulted for a granted read"
    assert own.commits_at == fleet.commits_at == granted.commits_at == []


async def test_a_member_without_a_grant_reads_only_their_own_commands(monkeypatch):
    """A company-managed machine is visible to everyone, but its history is
    not: a plain member is scoped to the commands they dispatched, and that
    scoped read is not an oversight read either."""
    redis = _Redis()
    _install_redis(monkeypatch, redis)
    member = _user(admin=False)
    session = _Session(_runner(owner=None, company_managed=True))

    await _read_history(member, session)

    assert "runner_commands.user_id" in session.history_where
    assert session.audits == [] and redis.exists_calls == [] and redis.set_calls == []


def _command_row(runner: Runner, **result) -> SimpleNamespace:
    """A history row; without ``result`` the column was not loaded, and
    reading it raises like a deferred column read outside the session."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        runner_id=runner.id,
        user_id=None,
        agent_id=None,
        conversation_id=None,
        assignment_run_id=None,
        kind="run_shell",
        payload={"command": "ls"},
        status="completed",
        result_file_id=None,
        error=None,
        created_at=None,
        dispatched_at=None,
        completed_at=None,
        **result,
    )


async def test_the_history_can_leave_the_results_out_unread(monkeypatch):
    """The Desktop card shows what ran and how it ended, never a result, and a
    result may hold 16 MiB of JSON: ``include_result=false``
    neither loads nor serves it, and the oversight read is audited the same."""
    _install_redis(monkeypatch, _Redis())
    admin = _user(admin=True)
    session = _Session(_runner(owner=uuid.uuid4()))
    session.history_rows = [_command_row(session.runner)]

    rows = await runners_api.list_runner_commands(
        str(session.runner.id),
        SimpleNamespace(headers={}, client=None),
        limit=20,
        include_result=False,
        user=admin,
        db=session,
    )

    assert [(row["status"], row["result"]) for row in rows] == [("completed", None)]
    selected = session.history_sql.split(" FROM ", 1)[0]
    assert not re.search(r"runner_commands\.result\b", selected), selected
    assert len(session.audits) == 1

    # Asked for (the default), every result is loaded and served as before.
    default = _Session(_runner(owner=admin.id))
    default.history_rows = [_command_row(default.runner, result={"stdout": "a"})]
    assert [row["result"] for row in await _read_history(admin, default)] == [{"stdout": "a"}]
    assert re.search(r"runner_commands\.result\b", default.history_sql.split(" FROM ", 1)[0])


async def test_a_machine_a_member_cannot_see_is_not_found():
    member = _user(admin=False)
    session = _Session(runner=None)

    with pytest.raises(HTTPException) as excinfo:
        await _read_history(member, session)

    assert excinfo.value.status_code == 404
    assert session.audits == []
