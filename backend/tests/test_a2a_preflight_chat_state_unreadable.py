"""The A2A pre-flight tells "chat is off" from "chat state could not be read" (#411).

Two delegations sent in one model round share the run's session. A read of the
AI switch or the licence that failed there answered ``False``, and the second
call was refused as ``chat_unavailable`` ("AI is disabled, or Community
Edition") although nothing was misconfigured. The pre-flight now reads the gate
in a session of its own and refuses an unreadable state with a named,
retryable reason.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from app.a2a.client import delegation
from app.services import ai_state


@pytest.fixture
def own_sessions(monkeypatch):
    """Record the sessions the pre-flight opens instead of reaching a database."""
    opened: list[object] = []

    @asynccontextmanager
    async def _session():
        session = SimpleNamespace(name=f"own-{len(opened)}")
        opened.append(session)
        yield session

    monkeypatch.setattr("app.core.database.async_session", _session)
    return opened


def _gate(monkeypatch, answer):
    seen: list[object] = []

    async def _chat(db, *, licensed=None):
        seen.append(db)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr("app.api.v1.platform.is_chat_enabled", _chat)
    return seen


@pytest.mark.asyncio
async def test_the_gate_is_read_in_a_session_of_its_own(monkeypatch, own_sessions):
    seen = _gate(monkeypatch, True)
    caller = object()
    assert await delegation._chat_available(caller) is True
    assert seen == own_sessions
    assert caller not in seen


@pytest.mark.asyncio
async def test_a_licence_read_that_raises_is_unreadable_not_unavailable(monkeypatch, own_sessions):
    _gate(monkeypatch, OperationalError("select", {}, Exception("aborted")))
    with pytest.raises(delegation.PreflightRefusedError) as caught:
        await delegation.preflight(object(), SimpleNamespace(), None)
    assert caught.value.reason == delegation.OUTCOME_CHAT_STATE_UNREADABLE
    assert caught.value.detail == delegation.CHAT_STATE_UNREADABLE_DETAIL
    assert "Community Edition" not in caught.value.detail


@pytest.mark.asyncio
async def test_a_failed_switch_read_is_unreadable_not_unavailable(monkeypatch, own_sessions):
    _gate(monkeypatch, False)
    monkeypatch.setattr(ai_state, "_cache", None)
    monkeypatch.setattr(ai_state, "_last_read_failed", True)
    with pytest.raises(delegation.PreflightRefusedError) as caught:
        await delegation.preflight(object(), SimpleNamespace(), None)
    assert caught.value.reason == delegation.OUTCOME_CHAT_STATE_UNREADABLE


@pytest.mark.asyncio
async def test_a_switch_read_as_off_is_still_chat_unavailable(monkeypatch, own_sessions):
    _gate(monkeypatch, False)
    monkeypatch.setattr(ai_state, "_cache", (False, float("inf")))
    monkeypatch.setattr(ai_state, "_last_read_failed", False)
    with pytest.raises(delegation.PreflightRefusedError) as caught:
        await delegation.preflight(object(), SimpleNamespace(), None)
    assert caught.value.reason == "chat_unavailable"


def test_every_test_starts_with_the_failed_read_flag_clear():
    # The flag is process-wide and ``invalidate_cache`` leaves it alone: the
    # autouse ``_reset_ai_state_cache`` fixture clears it, or a failed read in
    # an earlier test on the same worker turns a "chat unavailable" refusal
    # into "could not be read" (merge_group run 44561564902).
    assert ai_state.last_read_failed() is False


def test_the_unreadable_refusal_is_retryable():
    assert delegation.OUTCOME_CHAT_STATE_UNREADABLE in delegation.RETRYABLE_PREFLIGHT_REASONS


@pytest.mark.asyncio
async def test_is_ai_enabled_records_a_failed_read_and_clears_it(monkeypatch):
    monkeypatch.setattr(ai_state, "_cache", None)

    async def _fails(_db):
        raise OperationalError("select", {}, Exception("aborted"))

    monkeypatch.setattr(ai_state, "_read_override_isolated", _fails)
    assert await ai_state.is_ai_enabled(object()) is False
    assert ai_state.last_read_failed() is True

    async def _reads(_db):
        return None

    monkeypatch.setattr(ai_state, "_read_override_isolated", _reads)
    await ai_state.is_ai_enabled(object())
    assert ai_state.last_read_failed() is False
    monkeypatch.setattr(ai_state, "_cache", None)
