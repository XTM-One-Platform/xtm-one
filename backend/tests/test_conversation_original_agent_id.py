"""Tests for ``Conversation.original_agent_id`` write-once attribution.

Pins both halves of the contract:

* The ``before_insert`` event listener mirrors ``agent_id`` into
  ``original_agent_id`` when the column is left unset by the caller, so
  every ``Conversation(...)`` constructor across the codebase inherits
  the same attribution behaviour without each call site having to
  remember to populate the column.

* The ``before_update`` event listener refuses to mutate a non-NULL
  ``original_agent_id`` (the "write-once" half) — a regression in any
  caller that accidentally tries to rewrite the column after handover /
  transfer / rollback emits a WARNING and reverts the change rather
  than raising mid-flush.

The migration shape is pinned in ``test_migration_integrity.py`` and
the ``test_alembic_single_head`` invariant; this file owns the
runtime contract.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import event

from app.models.chat import (
    Conversation,
    _conversation_protect_original_agent_on_update,
    _conversation_set_original_agent_on_insert,
)


def _fake_mapper_connection():
    """Return a (mapper, connection) tuple compatible with the listeners.

    The listeners only inspect ``target``; mapper and connection are
    ignored, so passing ``None`` for both is sufficient and avoids
    needing a live SQLAlchemy session for the contract tests.
    """
    return (None, None)


# ── before_insert listener ─────────────────────────────────────────────


def test_before_insert_mirrors_agent_id_when_original_unset():
    agent_id = uuid.uuid4()
    conv = Conversation(agent_id=agent_id)
    assert conv.original_agent_id is None  # constructor leaves it unset

    mapper, conn = _fake_mapper_connection()
    _conversation_set_original_agent_on_insert(mapper, conn, conv)

    assert conv.original_agent_id == agent_id


def test_before_insert_preserves_caller_provided_original():
    """If a caller explicitly sets ``original_agent_id`` (e.g. backfill
    fixtures or a future code path that needs to attribute to a
    different agent than the current ``agent_id``), the listener must
    NOT clobber it."""
    agent_id = uuid.uuid4()
    explicit_original = uuid.uuid4()
    conv = Conversation(agent_id=agent_id, original_agent_id=explicit_original)

    mapper, conn = _fake_mapper_connection()
    _conversation_set_original_agent_on_insert(mapper, conn, conv)

    assert conv.original_agent_id == explicit_original


def test_before_insert_leaves_null_when_no_agent_id():
    """Conversations created without an agent (General Assistant
    route) MUST stay NULL on both columns. Listener noop."""
    conv = Conversation()

    mapper, conn = _fake_mapper_connection()
    _conversation_set_original_agent_on_insert(mapper, conn, conv)

    assert conv.agent_id is None
    assert conv.original_agent_id is None


# ── before_update listener ─────────────────────────────────────────────


class _FakeHistory:
    def __init__(self, deleted):
        self.deleted = deleted


class _FakeAttr:
    def __init__(self, deleted):
        self.history = _FakeHistory(deleted)


class _FakeAttrs:
    def __init__(self, original_agent_id_history):
        self.original_agent_id = _FakeAttr(original_agent_id_history)


class _FakeState:
    def __init__(self, original_agent_id_history):
        self.attrs = _FakeAttrs(original_agent_id_history)


def _drive_update(monkeypatch, conv, prior_value):
    """Drive ``before_update`` with a synthetic SQLAlchemy state.

    Stubs ``sqlalchemy.inspect`` so we don't need a live session — the
    listener only reads ``state.attrs.original_agent_id.history.deleted``,
    so a tiny fake covers the surface end-to-end.
    """
    import sqlalchemy

    # Wrap the prior value in a single-element list to mirror the
    # SQLAlchemy ``History.deleted`` shape, regardless of whether the
    # value is None (NULL → set promotion case) or an actual UUID
    # (set → set rejection case).
    fake_state = _FakeState(original_agent_id_history=[prior_value])
    monkeypatch.setattr(sqlalchemy, "inspect", lambda _t: fake_state)

    mapper, conn = _fake_mapper_connection()
    _conversation_protect_original_agent_on_update(mapper, conn, conv)


def test_before_update_reverts_mutation_of_set_original(monkeypatch, caplog):
    original = uuid.uuid4()
    rogue = uuid.uuid4()
    conv = Conversation(id=uuid.uuid4(), agent_id=uuid.uuid4(), original_agent_id=rogue)

    with caplog.at_level(logging.WARNING, logger="app.models.chat"):
        _drive_update(monkeypatch, conv, prior_value=original)

    # The listener MUST have reverted ``original_agent_id`` back to the
    # value it had before this update attempt.
    assert conv.original_agent_id == original
    # AND emitted a WARNING that names both the prior and rogue value
    # plus the conversation id, so on-call can grep for the offender.
    assert any(
        "write-once" in record.message
        and "Conversation.original_agent_id" in record.message
        for record in caplog.records
    ), f"expected write-once WARNING; got: {[r.message for r in caplog.records]}"


def test_before_update_permits_null_to_value_promotion(monkeypatch):
    """A row that was NULL before this update is being backfilled for
    the first time — the listener must permit this promotion (e.g. a
    row created before the column existed, or before the migration
    backfilled it). Only set→set mutations are blocked."""
    new_value = uuid.uuid4()
    conv = Conversation(id=uuid.uuid4(), agent_id=uuid.uuid4(), original_agent_id=new_value)

    _drive_update(monkeypatch, conv, prior_value=None)

    # NULL → set promotion: kept as-is.
    assert conv.original_agent_id == new_value


def test_before_update_noop_when_column_unchanged(monkeypatch):
    """When the update doesn't touch ``original_agent_id`` at all,
    ``history.deleted`` is empty and the listener noops."""
    original = uuid.uuid4()
    conv = Conversation(id=uuid.uuid4(), agent_id=uuid.uuid4(), original_agent_id=original)

    # Drive with empty history (column not in the update set).
    import sqlalchemy

    fake_state = _FakeState(original_agent_id_history=[])
    monkeypatch.setattr(sqlalchemy, "inspect", lambda _t: fake_state)

    mapper, conn = _fake_mapper_connection()
    _conversation_protect_original_agent_on_update(mapper, conn, conv)

    assert conv.original_agent_id == original


# ── Listener registration ──────────────────────────────────────────────


def test_listeners_are_registered_on_conversation():
    """A future refactor that drops ``event.listen(...)`` registration
    silently disables the entire write-once contract. Pin both
    registrations directly."""
    assert event.contains(
        Conversation,
        "before_insert",
        _conversation_set_original_agent_on_insert,
    ), "before_insert listener for original_agent_id is not registered"
    assert event.contains(
        Conversation,
        "before_update",
        _conversation_protect_original_agent_on_update,
    ), "before_update listener for original_agent_id is not registered"
