"""Standing approval persistence.

"Yes, always" writes a preference belonging to the person who clicked it. These
tests pin the upsert and fingerprint semantics that decide when a user gets
prompted again.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api.v1.assignments import _grant_standing_approvals
from app.api.v1.schemas.assignment import ApprovalDecision
from app.models.tool_approval import ToolApprovalPreference
from app.services.tool_approval_service import (
    ApprovalPreferencesUnavailableError,
    list_active_exceptions,
)
from app.tools.approval import fingerprint

SCHEMA = {"type": "object", "properties": {"target": {"type": "string"}}}


class _FakeDB:
    """Records added objects; ``scalar`` returns a queued existing row.

    The preference service also flushes (so a new row has an id to audit) and
    resolves the owning user (to attribute the audit record), so the stub
    answers both — the second and later ``scalar`` calls are the user lookup.
    """

    def __init__(self, existing=None, raises=None):
        self.added = []
        self._existing = existing
        self._raises = raises
        self._scalars = 0

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def scalar(self, _stmt):
        self._scalars += 1
        if self._scalars == 1:
            return self._existing
        return SimpleNamespace(id=uuid.uuid4(), email="reviewer@example.com")

    async def execute(self, _stmt):
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(all=lambda: [("delete_entity", "fp-1", "builtin:test", None)])


def _run(*proposals):
    return SimpleNamespace(pending_approvals=list(proposals))


def _proposal(call_id="c1", tool_name="delete_entity", schema=SCHEMA):
    return {
        "tool_call_id": call_id,
        "tool_name": tool_name,
        "arguments": {},
        "input_schema": schema,
        "source": "builtin:test",
    }


def _user(impersonated_by=None):
    user = SimpleNamespace(id=uuid.uuid4())
    if impersonated_by is not None:
        user.impersonated_by = impersonated_by
    return user


def _decision(call_id="c1", decision="approve_always"):
    return ApprovalDecision(tool_call_id=call_id, decision=decision)


async def test_approve_always_records_a_preference():
    db, run, user = _FakeDB(), _run(_proposal()), _user()

    await _grant_standing_approvals(db, run, [_decision()], user)

    # Filtered by type: granting also writes an audit record (FR-018), so a
    # bare length check would now count the trail as a second preference.
    prefs = [o for o in db.added if isinstance(o, ToolApprovalPreference)]
    assert len(prefs) == 1
    assert prefs[0].tool_key == "delete_entity"
    assert prefs[0].user_id == user.id
    assert prefs[0].tool_source == "builtin:test"


@pytest.mark.parametrize("verdict", ["approve", "reject"])
async def test_other_verdicts_record_nothing(verdict):
    """Only "always" is standing — a one-off approval must not silence future prompts."""
    db, run, user = _FakeDB(), _run(_proposal()), _user()

    await _grant_standing_approvals(db, run, [_decision(decision=verdict)], user)

    assert db.added == []


async def test_fingerprint_captures_the_schema_the_user_was_shown():
    """Consent describes the call the reviewer saw, not the tool's current state.

    If the tool changed between the pause and the decision, the approval must
    not silently cover the new shape.
    """
    db, run, user = _FakeDB(), _run(_proposal()), _user()

    await _grant_standing_approvals(db, run, [_decision()], user)

    assert db.added[0].schema_fingerprint == fingerprint(SCHEMA)


async def test_regrant_updates_the_existing_row_rather_than_duplicating():
    existing = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        tool_key="delete_entity",
        schema_fingerprint="stale",
        tool_source="old",
        tool_entity_id=None,
        granted_by_admin_id=None,
    )
    db, run, user = _FakeDB(existing=existing), _run(_proposal()), _user()

    await _grant_standing_approvals(db, run, [_decision()], user)

    prefs = [o for o in db.added if isinstance(o, ToolApprovalPreference)]
    assert prefs == [], "a duplicate preference row was inserted"
    assert existing.schema_fingerprint == fingerprint(SCHEMA)
    assert existing.tool_source == "builtin:test"


async def test_impersonating_admin_is_recorded():
    """Both identities, so the trail shows who really made the change."""
    admin_id = uuid.uuid4()
    db, run, user = _FakeDB(), _run(_proposal()), _user(impersonated_by=admin_id)

    await _grant_standing_approvals(db, run, [_decision()], user)

    assert db.added[0].granted_by_admin_id == admin_id


async def test_decision_for_an_unknown_proposal_is_skipped():
    db, run, user = _FakeDB(), _run(_proposal(call_id="c1")), _user()

    await _grant_standing_approvals(db, run, [_decision(call_id="c99")], user)

    assert db.added == []


async def test_list_active_exceptions_returns_a_fingerprint_map():
    result = await list_active_exceptions(_FakeDB(), uuid.uuid4())

    assert result == {"delete_entity": "fp-1"}


async def test_list_active_exceptions_reports_the_tool_each_was_granted_to():
    sources: dict[str, str] = {}

    await list_active_exceptions(_FakeDB(), uuid.uuid4(), sources=sources)

    assert sources == {"delete_entity": "builtin:test"}


async def test_list_active_exceptions_without_a_user_is_empty():
    """Owner-less company-managed runs have no preferences, so everything gates."""
    assert await list_active_exceptions(_FakeDB(), None) == {}


async def test_lookup_failure_raises_rather_than_returning_empty():
    """An empty map means "approved nothing" — safe, but not the same as a failed read."""
    db = _FakeDB(raises=RuntimeError("connection reset"))

    with pytest.raises(ApprovalPreferencesUnavailableError):
        await list_active_exceptions(db, uuid.uuid4())


@pytest.mark.asyncio
async def test_a_lost_audit_write_increments_an_alertable_counter(monkeypatch):
    """The audit write is best-effort — failing a user's click to protect the
    record would be the wrong trade — but ``grant_exception`` is the only writer
    of consent, so a lost row means a standing approval exists with nothing
    saying who granted it. A log line nobody queries is not a detection.
    """
    import app.services.tool_approval_service as svc

    async def _boom(*_a, **_k):
        raise RuntimeError("audit table unavailable")

    monkeypatch.setattr("app.services.audit.log_audit", _boom)

    def _before():
        for metric in svc._audit_write_failures.collect():
            for sample in metric.samples:
                if sample.labels.get("action") == "approval_granted":
                    return sample.value
        return 0.0

    start = _before()

    class _DB:
        begin_nested = None

        async def scalar(self, *_a, **_k):
            return SimpleNamespace(id=uuid.uuid4(), email="who@example.com")

    await svc._audit_preference_change(
        _DB(),
        SimpleNamespace(
            tool_key="notion:delete_page",
            tool_source="integration",
            user_id=uuid.uuid4(),
            id=uuid.uuid4(),
            granted_by_admin_id=None,
        ),
        action="approval_granted",
        summary="granted",
    )

    assert _before() == start + 1, "the lost write must be countable"
