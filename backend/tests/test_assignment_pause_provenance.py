"""Tests for assignment pause provenance.

``Assignment.enabled = False`` renders as a bare "Paused" badge, and six
unrelated code paths write it. These tests pin down that every one of them now
leaves a record of WHO/WHAT/WHEN/WHY, that a resume clears it, and that the
bookkeeping can never break the state change it describes.

Covered:
  * ``pause_assignment`` — stamps every column, tolerates an unknown reason,
    truncates an oversized label.
  * ``clear_pause_provenance`` / ``resume_assignment`` — the stamp is dropped
    on re-enable so an enabled assignment never claims to be paused.
  * ``record_pause_audit`` / ``resume_assignment`` — the ``pause`` / ``resume``
    audit rows, and the guarantee that an audit failure is swallowed.
  * The two autonomous callers (auth quarantine, invalid cron) actually stamp
    their reason.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.assignment_pause import (
    PAUSE_REASONS,
    clear_pause_provenance,
    pause_assignment,
    record_pause_audit,
    resume_assignment,
)


def _assignment(*, enabled: bool = True) -> MagicMock:
    """A stand-in assignment carrying only the attributes these paths touch."""
    assignment = MagicMock()
    assignment.id = uuid.uuid4()
    assignment.agent_id = uuid.uuid4()
    assignment.name = "Nightly exposure briefing"
    assignment.trigger_type = "schedule"
    assignment.enabled = enabled
    assignment.paused_at = None
    assignment.paused_reason = None
    assignment.paused_by_user_id = None
    assignment.paused_by_label = None
    assignment.paused_details = None
    return assignment


class _FakeSavepoint:
    """Async CM standing in for ``AsyncSession.begin_nested()``.

    Records whether it was entered and whether it exited with an exception, so
    a test can assert an audit failure was actually confined to a SAVEPOINT
    rather than left to poison the outer transaction.
    """

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        self._session.savepoints_entered += 1
        return self

    async def __aexit__(self, exc_type, _exc, _tb):
        if exc_type is not None:
            self._session.savepoints_rolled_back += 1
        return False  # never swallow — the caller's try/except decides


class _FakeSession:
    """Minimal async session: the parts ``assignment_pause`` actually touches."""

    def __init__(self):
        self.flushes = 0
        self.commits = 0
        self.savepoints_entered = 0
        self.savepoints_rolled_back = 0

    async def flush(self):
        self.flushes += 1

    def begin_nested(self):
        return _FakeSavepoint(self)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj):
        return None


def _user() -> MagicMock:
    user = MagicMock()
    user.id = uuid.uuid4()
    user.email = "analyst@example.com"
    user.full_name = "Ana Lyst"
    return user


# ---------------------------------------------------------------------------
# pause_assignment
# ---------------------------------------------------------------------------


def test_pause_stamps_every_column():
    assignment = _assignment()
    actor_id = uuid.uuid4()
    now = datetime(2026, 8, 29, 10, 30, tzinfo=UTC)

    pause_assignment(
        assignment,
        reason="user_toggle",
        details="Paused from the UI by Ana Lyst.",
        actor_user_id=actor_id,
        actor_label="analyst@example.com",
        now=now,
    )

    assert assignment.enabled is False
    assert assignment.paused_at == now
    assert assignment.paused_reason == "user_toggle"
    assert assignment.paused_by_user_id == actor_id
    assert assignment.paused_by_label == "analyst@example.com"
    assert assignment.paused_details == "Paused from the UI by Ana Lyst."


def test_pause_without_actor_is_a_system_pause():
    """An autonomous pause has no human to blame — but still a label."""
    assignment = _assignment()

    pause_assignment(
        assignment,
        reason="auth_quarantine",
        details="Auto-paused after 3 consecutive run(s).",
        actor_user_id=None,
        actor_label="XTM One — assignment engine",
    )

    assert assignment.paused_by_user_id is None
    assert assignment.paused_by_label == "XTM One — assignment engine"
    assert assignment.paused_at is not None


def test_pause_accepts_string_actor_id():
    """Callers holding a stringified id (builder tools) must not have to cast."""
    assignment = _assignment()
    actor_id = uuid.uuid4()

    pause_assignment(
        assignment,
        reason="agent_tool",
        details="Paused by an agent.",
        actor_user_id=str(actor_id),
        actor_label="analyst@example.com",
    )

    assert assignment.paused_by_user_id == actor_id


def test_pause_with_unknown_reason_still_records():
    """A typo must not cost us the pause record — store it and warn."""
    assignment = _assignment()

    pause_assignment(
        assignment,
        reason="typo_reason",
        details="Something paused it.",
        actor_user_id=None,
        actor_label="somewhere",
    )

    assert assignment.enabled is False
    assert assignment.paused_reason == "typo_reason"
    assert "typo_reason" not in PAUSE_REASONS


def test_created_disabled_is_a_known_reason():
    """A duplicate / import / catalog install is born paused ON PURPOSE.

    Without a reason code for it, those copies would render as "paused, cause
    unknown" — the exact ambiguity this feature removes.
    """
    assignment = _assignment()

    pause_assignment(
        assignment,
        reason="created_disabled",
        details="Created switched off: duplicated from 'Nightly briefing'.",
        actor_user_id=uuid.uuid4(),
        actor_label="analyst@example.com",
    )

    assert assignment.paused_reason in PAUSE_REASONS
    assert assignment.enabled is False


def test_pause_truncates_an_oversized_label():
    """String(320) column — truncate rather than fail the write."""
    assignment = _assignment()

    pause_assignment(
        assignment,
        reason="user_toggle",
        details="d",
        actor_user_id=None,
        actor_label="x" * 500,
    )

    assert len(assignment.paused_by_label) == 320


def test_pause_truncates_an_oversized_unknown_reason():
    """String(32) column — an overlong typo must not lose the whole record.

    The tolerant branch above exists so a bad reason code still records the
    pause; letting it overflow the column would raise on flush and lose it,
    which is the very outcome that tolerance is there to prevent.
    """
    assignment = _assignment()

    pause_assignment(
        assignment,
        reason="a_reason_far_longer_than_the_column_allows",
        details="d",
        actor_user_id=None,
        actor_label="somewhere",
    )

    assert assignment.enabled is False
    assert len(assignment.paused_reason) == 32


def test_pause_normalizes_empty_label_and_details_to_none():
    assignment = _assignment()

    pause_assignment(
        assignment,
        reason="user_toggle",
        details="",
        actor_user_id=None,
        actor_label="",
    )

    assert assignment.paused_by_label is None
    assert assignment.paused_details is None


# ---------------------------------------------------------------------------
# clear_pause_provenance / resume_assignment
# ---------------------------------------------------------------------------


def test_clear_drops_the_whole_stamp():
    assignment = _assignment()
    pause_assignment(
        assignment,
        reason="user_toggle",
        details="d",
        actor_user_id=uuid.uuid4(),
        actor_label="analyst@example.com",
    )

    clear_pause_provenance(assignment)

    assert assignment.paused_at is None
    assert assignment.paused_reason is None
    assert assignment.paused_by_user_id is None
    assert assignment.paused_by_label is None
    assert assignment.paused_details is None


@pytest.mark.asyncio
async def test_resume_reenables_clears_and_audits():
    assignment = _assignment()
    pause_assignment(
        assignment,
        reason="auth_quarantine",
        details="Auto-paused after 3 consecutive run(s).",
        actor_user_id=None,
        actor_label="XTM One — assignment engine",
    )
    user = _user()
    db = _FakeSession()

    with patch(
        "app.services.audit.log_audit", new_callable=AsyncMock
    ) as log_audit:
        await resume_assignment(db, assignment=assignment, user=user)

    assert assignment.enabled is True
    assert assignment.paused_reason is None
    assert assignment.paused_at is None
    log_audit.assert_awaited_once()
    kwargs = log_audit.await_args.kwargs
    assert kwargs["action"] == "resume"
    assert kwargs["entity_type"] == "assignment"
    # The resume row carries the reason it is undoing — "paused since Tuesday"
    # and "paused/resumed four times today" are different problems.
    assert kwargs["details"]["reason"] == "auth_quarantine"


# ---------------------------------------------------------------------------
# record_pause_audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_pause_audit_writes_a_pause_row():
    assignment = _assignment(enabled=False)
    user = _user()
    db = _FakeSession()

    with patch(
        "app.services.audit.log_audit", new_callable=AsyncMock
    ) as log_audit:
        await record_pause_audit(
            db,
            assignment=assignment,
            user=user,
            reason="user_toggle",
            details="Paused from the UI by Ana Lyst.",
        )

    kwargs = log_audit.await_args.kwargs
    assert kwargs["action"] == "pause"
    assert kwargs["entity_id"] == assignment.id
    assert kwargs["details"]["reason"] == "user_toggle"
    assert kwargs["details"]["reason_label"] == PAUSE_REASONS["user_toggle"]
    assert PAUSE_REASONS["user_toggle"] in kwargs["summary"]


@pytest.mark.asyncio
async def test_audit_failure_is_confined_to_a_savepoint():
    """The trail is best-effort; the pause it describes is not.

    Swallowing the exception is not sufficient on its own: ``log_audit`` ends
    in ``db.flush()``, and a failed flush leaves the session needing a
    rollback, so the caller's next ``commit()`` would raise
    ``PendingRollbackError`` — losing the pause. The write must therefore
    happen inside a SAVEPOINT, and the caller's own state must be flushed
    before it so it lands in the outer transaction regardless.
    """
    assignment = _assignment(enabled=False)
    user = _user()
    db = _FakeSession()

    with patch(
        "app.services.audit.log_audit",
        new_callable=AsyncMock,
        side_effect=RuntimeError("audit table is on fire"),
    ):
        await record_pause_audit(
            db,
            assignment=assignment,
            user=user,
            reason="user_toggle",
            details="d",
        )
        # Same guarantee on the resume side.
        await resume_assignment(db, assignment=assignment, user=user)

    assert assignment.enabled is True
    # The caller's state reached the outer transaction before the audit write.
    assert db.flushes == 2
    # Both failures were contained by a SAVEPOINT, not left on the session.
    assert db.savepoints_entered == 2
    assert db.savepoints_rolled_back == 2


# ---------------------------------------------------------------------------
# The autonomous callers stamp their reason
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_quarantine_stamps_its_reason(monkeypatch):
    from app.core.config import settings
    from app.services.assignment_engine import _track_assignment_auth_failures

    monkeypatch.setattr(settings, "assignment_auth_fail_quarantine_threshold", 3)

    assignment = _assignment()
    assignment.user_id = uuid.uuid4()
    assignment.is_company_managed = False
    assignment.run_as_all_users = False
    agent = MagicMock()
    agent.is_builtin = False
    run = MagicMock()
    run.trigger_context = {"type": "schedule"}

    auth_fail = MagicMock()
    auth_fail.role = "tool"
    auth_fail.content = (
        "Error: authentication failed — the token may be expired or revoked."
    )

    with (
        patch(
            "app.services.assignment_engine._record_assignment_auth_failure",
            new_callable=AsyncMock,
            return_value=3,
        ),
        patch(
            "app.services.assignment_engine._handle_assignment_auth_failure",
            new_callable=AsyncMock,
        ),
    ):
        await _track_assignment_auth_failures(
            AsyncMock(),
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-1",
            messages=[auth_fail],
        )

    assert assignment.enabled is False
    assert assignment.paused_reason == "auth_quarantine"
    # No human did this — the label names the component instead.
    assert assignment.paused_by_user_id is None
    assert "engine" in assignment.paused_by_label
    assert "re-authorize" in assignment.paused_details.lower()


@pytest.mark.asyncio
async def test_invalid_cron_stamps_its_reason():
    from app.services import autonomous

    now = datetime(2026, 8, 29, 3, 0, tzinfo=UTC)
    assignment = _assignment()
    assignment.user_id = uuid.uuid4()
    assignment.run_as_user_id = None
    assignment.is_company_managed = False

    db = MagicMock()
    db.commit = AsyncMock()

    await autonomous._flag_assignment_cron_misconfiguration(
        db=db,
        assignment=assignment,
        cron_expr="*/15 * * 1",
        cron_err=ValueError("Exactly 5, 6 or 7 columns has to be specified"),
        now=now,
    )

    assert assignment.enabled is False
    assert assignment.paused_reason == "invalid_schedule"
    assert assignment.paused_at == now
    assert assignment.paused_by_user_id is None
    assert "*/15 * * 1" in assignment.paused_details


# ---------------------------------------------------------------------------
# The paths a user actually exercises
#
# The service-level tests above pin the stamping rules; these pin the callers,
# which is where the branching lives — the REST route's edge detection, the
# builder tool's unresolvable-actor fallback, and seeding's two disable
# branches. Every one of them was previously untested.
# ---------------------------------------------------------------------------


class _RestAgentResult:
    def __init__(self, agent):
        self._agent = agent

    def scalar_one_or_none(self):
        return self._agent


class _RestDB(_FakeSession):
    """``_FakeSession`` plus the parent-agent lookup, which is the only query
    the route runs for an ``enabled``-only payload."""

    def __init__(self, agent):
        super().__init__()
        self._agent = agent

    async def execute(self, _stmt):
        return _RestAgentResult(self._agent)


def _rest_assignment(*, enabled: bool) -> MagicMock:
    assignment = _assignment(enabled=enabled)
    assignment.delay = None
    assignment.ai_label = False
    assignment.human_in_the_loop = False
    assignment.max_iterations = None
    return assignment


async def _invoke_update(monkeypatch, assignment, body, user, *, audit=None, db=None):
    """Drive the real ``update_assignment`` route with everything but the
    pause bookkeeping stubbed out."""
    from fastapi import Request

    from app.api.v1 import assignments as assignments_api

    agent = MagicMock()
    agent.id = assignment.agent_id
    agent.is_company_managed = False
    agent.name = "Agent"

    async def _groups(_db, _uid):
        return []

    async def _get_assignment(_db, _uid, _aid, _gids, **_kwargs):
        return assignment

    monkeypatch.setattr(assignments_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(assignments_api, "get_user_assignment", _get_assignment)
    monkeypatch.setattr(
        assignments_api, "assert_writable_assignment", lambda *a, **k: None
    )
    monkeypatch.setattr(assignments_api, "log_audit", audit or AsyncMock())
    monkeypatch.setattr(
        assignments_api, "_to_response", lambda w, **_k: {"id": str(w.id)}
    )

    request = Request({"type": "http", "method": "PUT", "path": "/x", "headers": []})
    db = db or _RestDB(agent)
    await assignments_api.update_assignment(
        str(assignment.id), body, request, user, db
    )
    return db


@pytest.mark.asyncio
async def test_rest_toggle_off_stamps_and_audits(monkeypatch):
    from app.api.v1.schemas.assignment import UpdateAssignmentRequest

    assignment = _rest_assignment(enabled=True)
    user = _user()

    with patch(
        "app.services.audit.log_audit", new_callable=AsyncMock
    ) as log_audit:
        await _invoke_update(
            monkeypatch, assignment, UpdateAssignmentRequest(enabled=False), user
        )

    assert assignment.enabled is False
    assert assignment.paused_reason == "user_toggle"
    assert assignment.paused_by_user_id == user.id
    # The label is the display name, not the email: it renders
    # to everyone who can read the assignment, a wider audience than the
    # admin-scoped audit log.
    assert assignment.paused_by_label == user.full_name
    assert log_audit.await_args.kwargs["action"] == "pause"


@pytest.mark.asyncio
async def test_rest_toggle_off_before_the_first_run_stays_not_set_up(monkeypatch):
    """#11060: a switch-off before any run is ``created_disabled``, so the fleet reads Not set up."""
    from app.api.v1.schemas.assignment import UpdateAssignmentRequest

    assignment = _rest_assignment(enabled=True)
    assignment.execution_count = 0
    assignment.last_triggered_at = None
    user = _user()

    with patch("app.services.audit.log_audit", new_callable=AsyncMock) as log_audit:
        await _invoke_update(monkeypatch, assignment, UpdateAssignmentRequest(enabled=False), user)

    assert assignment.enabled is False
    assert assignment.paused_reason == "created_disabled"
    assert assignment.paused_by_user_id == user.id
    assert log_audit.await_args.kwargs["details"]["reason"] == "created_disabled"


@pytest.mark.asyncio
async def test_rest_toggle_on_clears_and_audits_resume(monkeypatch):
    from app.api.v1.schemas.assignment import UpdateAssignmentRequest

    assignment = _rest_assignment(enabled=False)
    pause_assignment(
        assignment,
        reason="auth_quarantine",
        details="Auto-paused.",
        actor_user_id=None,
        actor_label="XTM One — assignment engine",
    )
    user = _user()

    with patch(
        "app.services.audit.log_audit", new_callable=AsyncMock
    ) as log_audit:
        await _invoke_update(
            monkeypatch, assignment, UpdateAssignmentRequest(enabled=True), user
        )

    assert assignment.enabled is True
    assert assignment.paused_reason is None
    assert assignment.paused_at is None
    assert log_audit.await_args.kwargs["action"] == "resume"


@pytest.mark.asyncio
async def test_rest_unrelated_edit_does_not_restamp(monkeypatch):
    """The edge-detection guard: a save carrying ``enabled`` unchanged must not
    move ``paused_at`` — otherwise every unrelated edit rewrites the history."""
    from app.api.v1.schemas.assignment import UpdateAssignmentRequest

    assignment = _rest_assignment(enabled=False)
    pause_assignment(
        assignment,
        reason="user_toggle",
        details="Paused from the UI by Ana Lyst.",
        actor_user_id=uuid.uuid4(),
        actor_label="Ana Lyst",
        now=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
    )
    original_paused_at = assignment.paused_at

    with patch("app.services.audit.log_audit", new_callable=AsyncMock):
        await _invoke_update(
            monkeypatch,
            assignment,
            UpdateAssignmentRequest(enabled=False, name="Renamed"),
            _user(),
        )

    assert assignment.paused_at == original_paused_at
    assert assignment.paused_details == "Paused from the UI by Ana Lyst."


# ---------------------------------------------------------------------------
# Seeding — both disable branches
# ---------------------------------------------------------------------------


def test_seed_disable_stamps_system_seed():
    """Seeding a built-in as disabled must not leave a bare "Paused" badge."""
    assignment = _assignment(enabled=True)

    pause_assignment(
        assignment,
        reason="system_seed",
        details="Disabled by startup seeding.",
        actor_user_id=None,
        actor_label="XTM One — startup seeding",
    )

    assert assignment.paused_reason == "system_seed"
    assert assignment.paused_by_user_id is None


def test_system_reasons_reject_a_human_actor():
    """An autonomous decision must never be attributed to a person."""
    from app.services.assignment_pause import SYSTEM_REASONS

    assignment = _assignment()
    assert "system_seed" in SYSTEM_REASONS

    pause_assignment(
        assignment,
        reason="auth_quarantine",
        details="Auto-paused.",
        actor_user_id=uuid.uuid4(),  # a caller mistake
        actor_label="XTM One — assignment engine",
    )

    assert assignment.paused_by_user_id is None


# ---------------------------------------------------------------------------
# Truncation against the REAL column widths, not a MagicMock
# ---------------------------------------------------------------------------


def test_truncation_matches_the_declared_column_widths():
    from app.models.assignment import Assignment as _Assignment

    columns = _Assignment.__table__.columns
    assert columns["paused_reason"].type.length == 32
    assert columns["paused_by_label"].type.length == 320

    real = _Assignment(
        agent_id=uuid.uuid4(), name="n", trigger_type="manual", prompt="p"
    )
    pause_assignment(
        real,
        reason="x" * 100,
        details="d",
        actor_user_id=None,
        actor_label="y" * 900,
    )

    assert len(real.paused_reason) <= columns["paused_reason"].type.length
    assert len(real.paused_by_label) <= columns["paused_by_label"].type.length


@pytest.mark.asyncio
async def test_rest_pause_survives_a_broken_audit_table(monkeypatch):
    """End-to-end version of the SAVEPOINT guarantee.

    A failing audit write must not stop the route from committing the pause —
    before the SAVEPOINT the poisoned session turned this into a 500 with the
    pause lost, which is the exact opposite of the module's contract.
    """
    from app.api.v1.schemas.assignment import UpdateAssignmentRequest

    assignment = _rest_assignment(enabled=True)

    with patch(
        "app.services.audit.log_audit",
        new_callable=AsyncMock,
        side_effect=RuntimeError("audit table is on fire"),
    ):
        db = await _invoke_update(
            monkeypatch,
            assignment,
            UpdateAssignmentRequest(enabled=False),
            _user(),
        )

    assert assignment.enabled is False
    assert assignment.paused_reason == "user_toggle"
    assert db.commits == 1
    assert db.savepoints_rolled_back >= 1


@pytest.mark.asyncio
async def test_update_and_its_audit_entry_land_in_one_commit(monkeypatch):
    """A failure writing the update's audit entry commits nothing.

    The update and its audit row are one transaction, committed once before
    the response: a failure between them surfaces as the route's error and
    the teardown rolls both back, instead of an update landing without it.
    """
    from app.api.v1.schemas.assignment import UpdateAssignmentRequest

    assignment = _rest_assignment(enabled=True)
    db = _RestDB(MagicMock(id=assignment.agent_id))

    with (
        patch("app.services.audit.log_audit", new_callable=AsyncMock),
        pytest.raises(RuntimeError, match="audit write failed"),
    ):
        await _invoke_update(
            monkeypatch,
            assignment,
            UpdateAssignmentRequest(enabled=False),
            _user(),
            audit=AsyncMock(side_effect=RuntimeError("audit write failed")),
            db=db,
        )

    assert db.commits == 0


@pytest.mark.asyncio
async def test_api_created_disabled_assignment_is_stamped(monkeypatch):
    """``POST /assignments`` was the one creation path with no stamp.

    ``CreateAssignmentRequest.enabled`` is client-settable, so an API-created
    disabled assignment used to render "paused before pause tracking was
    added" about a row created seconds ago.
    """
    from fastapi import Request

    from app.api.v1 import assignments as assignments_api
    from app.api.v1.schemas.assignment import CreateAssignmentRequest

    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.user_id = uuid.uuid4()
    agent.is_company_managed = False
    agent.builtin_tool_ids = []

    added: list = []

    class _CreateDB(_FakeSession):
        def add(self, obj):
            added.append(obj)

    async def _groups(_db, _uid):
        return []

    async def _get_agent(_db, _uid, _aid, _gids, **_kwargs):
        return agent

    monkeypatch.setattr(assignments_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(assignments_api, "get_user_agent", _get_agent)
    monkeypatch.setattr(
        assignments_api, "assert_writable_agent", lambda *a, **k: None
    )
    monkeypatch.setattr(
        assignments_api, "_resolve_run_as_user_id", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(assignments_api, "log_audit", AsyncMock())
    monkeypatch.setattr(
        assignments_api, "_to_response", lambda w, **_k: {"id": str(w.id)}
    )
    monkeypatch.setattr(assignments_api, "_owner_paused", AsyncMock(return_value=False))

    user = _user()
    request = Request({"type": "http", "method": "POST", "path": "/x", "headers": []})
    await assignments_api.create_assignment(
        CreateAssignmentRequest(
            agent_id=str(agent.id),
            name="Draft",
            prompt="Do the thing.",
            trigger_type="manual",
            enabled=False,
        ),
        request,
        user,
        _CreateDB(),
    )

    created = added[0]
    assert created.enabled is False
    assert created.paused_reason == "created_disabled"
    assert created.paused_by_user_id == user.id
    assert created.paused_by_label == user.full_name


@pytest.mark.asyncio
async def test_api_created_enabled_assignment_carries_no_stamp(monkeypatch):
    """The normal create must not stamp anything — an enabled assignment that
    claimed to be paused would be the mirror image of the original bug."""
    from fastapi import Request

    from app.api.v1 import assignments as assignments_api
    from app.api.v1.schemas.assignment import CreateAssignmentRequest

    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.user_id = uuid.uuid4()
    agent.is_company_managed = False
    agent.builtin_tool_ids = []

    added: list = []

    class _CreateDB(_FakeSession):
        def add(self, obj):
            added.append(obj)

    monkeypatch.setattr(
        assignments_api, "get_user_group_ids", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        assignments_api, "get_user_agent", AsyncMock(return_value=agent)
    )
    monkeypatch.setattr(
        assignments_api, "assert_writable_agent", lambda *a, **k: None
    )
    monkeypatch.setattr(
        assignments_api, "_resolve_run_as_user_id", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(assignments_api, "log_audit", AsyncMock())
    monkeypatch.setattr(
        assignments_api, "_to_response", lambda w, **_k: {"id": str(w.id)}
    )
    monkeypatch.setattr(assignments_api, "_owner_paused", AsyncMock(return_value=False))

    request = Request({"type": "http", "method": "POST", "path": "/x", "headers": []})
    await assignments_api.create_assignment(
        CreateAssignmentRequest(
            agent_id=str(agent.id),
            name="Live",
            prompt="Do the thing.",
            trigger_type="manual",
        ),
        request,
        _user(),
        _CreateDB(),
    )

    created = added[0]
    assert created.enabled is True
    assert created.paused_reason is None
    assert created.paused_at is None


def test_expected_reasons_log_at_info(caplog):
    """Routine pauses must not emit WARNING — duplicating an agent with N
    assignments would otherwise produce N warnings for expected behaviour."""
    import logging

    from app.services.assignment_pause import EXPECTED_REASONS

    assert set(EXPECTED_REASONS) == {"created_disabled", "system_seed"}

    with caplog.at_level(logging.INFO, logger="app.services.assignment_pause"):
        pause_assignment(
            _assignment(),
            reason="created_disabled",
            details="Created switched off.",
            actor_user_id=None,
            actor_label="somewhere",
        )
    assert [r.levelname for r in caplog.records] == ["INFO"]

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.services.assignment_pause"):
        pause_assignment(
            _assignment(),
            reason="auth_quarantine",
            details="Auto-paused.",
            actor_user_id=None,
            actor_label="XTM One — assignment engine",
        )
    assert "WARNING" in [r.levelname for r in caplog.records]


def test_unflushed_assignment_logs_without_a_none_id(caplog):
    """The copy / import / seed-insert paths reach the logger before flush, so
    the PK default has not been applied yet."""
    import logging

    from app.models.assignment import Assignment as _Assignment

    unsaved = _Assignment(
        agent_id=uuid.uuid4(), name="n", trigger_type="manual", prompt="p"
    )
    assert unsaved.id is None

    with caplog.at_level(logging.INFO, logger="app.services.assignment_pause"):
        pause_assignment(
            unsaved,
            reason="created_disabled",
            details="Created switched off.",
            actor_user_id=None,
            actor_label="somewhere",
        )

    assert "unsaved" in caplog.records[0].getMessage()
    assert "None" not in caplog.records[0].getMessage()
