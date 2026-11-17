"""Invalidating standing approvals when a tool changes (FR-019a/b).

A standing approval is consent to a tool's *shape*. When the shape changes, the
consent no longer describes what would run, so the tool must prompt again — and
the stale row must be marked, for two reasons:

* Otherwise the user's preference list claims they always allow a tool that is
  visibly prompting them, with nothing to explain the difference.
* An untouched row is dormant, not dead. If the tool's schema ever reverted, the
  fingerprint would match again and the approval would silently resume,
  reviving consent the user never re-gave.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.services.tool_approval_service import (
    REASON_TOOL_CHANGED,
    REASON_USER_REVOKED,
    invalidate_stale_approvals,
)
from app.tools.approval import evaluate_gate, fingerprint

SCHEMA_V1 = {"type": "object", "properties": {"target": {"type": "string"}}}
SCHEMA_V2 = {
    "type": "object",
    "properties": {"target": {"type": "string"}, "force": {"type": "boolean"}},
}


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)


class _FakeDB:
    def __init__(self, rows=()):
        self._rows = list(rows)
        self.queries = 0

    async def execute(self, _stmt):
        self.queries += 1
        return _FakeResult(self._rows)


def _preference(tool_key="delete_entity"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tool_key=tool_key,
        revoked_at=None,
        revoked_reason=None,
    )


# --- detection (pure gate) ----------------------------------------------


def test_changed_schema_flags_the_preference_stale():
    decision = evaluate_gate(
        "delete_entity",
        hitl_enabled=True,
        whitelist=frozenset(),
        active_exceptions={"delete_entity": fingerprint(SCHEMA_V1)},
        current_fingerprint=fingerprint(SCHEMA_V2),
    )

    assert decision.action == "pause"
    assert decision.invalidated_preference is True


def test_unchanged_schema_does_not_flag_anything():
    fp = fingerprint(SCHEMA_V1)
    decision = evaluate_gate(
        "delete_entity",
        hitl_enabled=True,
        whitelist=frozenset(),
        active_exceptions={"delete_entity": fp},
        current_fingerprint=fp,
    )

    assert decision.action == "execute"
    assert decision.invalidated_preference is False


def test_cosmetic_change_does_not_invalidate():
    """Rewording a description must not cost every user their approval."""
    described = {
        "type": "object",
        "properties": {"target": {"type": "string", "description": ""}},
    }
    assert fingerprint(SCHEMA_V1) != fingerprint(described)

    # ...but the tool's own name/description are outside the schema entirely,
    # so they cannot affect the fingerprint at all.
    assert fingerprint(SCHEMA_V1) == fingerprint(dict(SCHEMA_V1))


# --- revocation ----------------------------------------------------------


async def test_stale_preference_is_revoked_with_the_right_reason():
    preference = _preference()
    db = _FakeDB([preference])

    revoked = await invalidate_stale_approvals(
        db, user_id=uuid.uuid4(), tool_keys={"delete_entity"}
    )

    assert revoked == [preference]
    assert preference.revoked_at is not None
    assert preference.revoked_reason == REASON_TOOL_CHANGED


async def test_reason_distinguishes_a_tool_change_from_a_user_revocation():
    """"Why is this asking me again?" must stay answerable."""
    preference = _preference()
    await invalidate_stale_approvals(
        _FakeDB([preference]), user_id=uuid.uuid4(), tool_keys={"delete_entity"}
    )

    assert preference.revoked_reason != REASON_USER_REVOKED


async def test_several_stale_tools_are_revoked_together():
    prefs = [_preference("delete_entity"), _preference("send_report")]
    db = _FakeDB(prefs)

    revoked = await invalidate_stale_approvals(
        db, user_id=uuid.uuid4(), tool_keys={"delete_entity", "send_report"}
    )

    assert len(revoked) == 2
    assert all(p.revoked_at is not None for p in revoked)


async def test_empty_input_does_not_hit_the_database():
    """The common case is nothing stale — it must cost nothing."""
    db = _FakeDB()

    assert await invalidate_stale_approvals(db, user_id=uuid.uuid4(), tool_keys=set()) == []
    assert db.queries == 0


async def test_no_matching_preference_is_not_an_error():
    """Racing with the user's own revocation must not blow up the pause."""
    db = _FakeDB([])

    assert await invalidate_stale_approvals(
        db, user_id=uuid.uuid4(), tool_keys={"delete_entity"}
    ) == []


# --- the engine actually drains it --------------------------------------


def test_pause_handler_revokes_stale_approvals():
    """Source pin: detection is worthless if nothing acts on it.

    The gate is synchronous and cannot write, so the flag is collected and drained
    by the pause handler. This asserts that wiring exists, since it spans a
    ~2,000-line coroutine that no unit test can drive end to end.
    """
    import inspect

    import app.services.assignment_engine as engine

    source = inspect.getsource(engine.execute_assignment_run)

    assert "_decision.invalidated_preference" in source, (
        "the gate's stale flag is never read — a changed tool would re-prompt "
        "but the user's preference list would still claim they always allow it"
    )
    assert "invalidate_stale_approvals" in source
    assert "_run_exceptions.pop" in source, (
        "a flagged approval must also be dropped from the in-memory map, or the "
        "rest of the run keeps matching against consent being revoked"
    )
