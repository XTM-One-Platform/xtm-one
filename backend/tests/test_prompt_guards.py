"""The prompt guard evaluates on a session of its own, bounded, fail-open.

A guard reads the database while it evaluates, inside a bound. A bound that
cancelled one of its statements on the REQUEST's session aborted the
request's transaction: the request failed on its next statement and lost
what it had written. ``evaluate_text`` therefore takes no session: it opens
one for the connection-state lookup and the evaluation, and closes it. With no
guard connected a turn opens no session at all once "none connected" is
known.

The database edge is a stand-in that counts the sessions opened and the
statements run; everything above it is the real code: the connection-state
query, the Nova engine and its keyword tier, and the chat-message audit.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import nova_engine, prompt_guards
from app.services.nova_guard import NOVA_REFUSAL_MESSAGE
from app.services.prompt_guards import (
    NOT_FLAGGED,
    SURFACE_CHAT_MESSAGE,
    evaluate_text,
    guard_chat_message,
)

pytestmark = pytest.mark.asyncio

_CANARY = "zebra-canary-7731"


def _rule_row(action: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=f"{action}-rule",
        action=action,
        rule_text=(
            f'rule Canary_{action} {{ meta: category = "Injection"\n'
            f'  keywords: $k = "{_CANARY}"\n'
            "  condition: keywords.$k }"
        ),
    )


class _Database:
    """``app.core.database.async_session``: counts sessions, statements and commits.

    Answers the connection-state query with a row while *connected*, and the
    rule query with *rules*; *execute* may be replaced to fail or stall.
    """

    def __init__(self, *, connected: bool, rules: list | None = None) -> None:
        self.connected = connected
        self.rules = rules or []
        self.opened = 0
        self.statements: list = []
        self.sessions: list = []
        self.commits = 0
        self.rollbacks = 0

    def __call__(self):
        return self._session()

    @contextlib.asynccontextmanager
    async def _session(self):
        self.opened += 1
        session = SimpleNamespace(execute=self.execute, commit=self.commit, rollback=self.rollback)
        self.sessions.append(session)
        yield session

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        row = (uuid.uuid4(),) if self.connected else None
        rules = self.rules
        return SimpleNamespace(first=lambda: row, scalars=lambda: SimpleNamespace(all=lambda: rules))

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def database(monkeypatch):
    def install(**kwargs) -> _Database:
        db = _Database(**kwargs)
        monkeypatch.setattr("app.core.database.async_session", db)
        return db

    # The keyword tier is the one CI has; pin it so the chain is the same everywhere.
    monkeypatch.setattr(nova_engine, "NOVA_HUNTING_AVAILABLE", False)
    return install


async def test_the_entry_point_takes_no_session():
    parameters = inspect.signature(evaluate_text).parameters
    assert list(parameters) == ["text", "surface", "user_id", "timeout"]
    assert parameters["text"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert all(parameters[name].kind is inspect.Parameter.KEYWORD_ONLY for name in ("surface", "user_id", "timeout"))


async def test_with_no_guard_connected_a_turn_opens_no_session_once_that_is_known(database):
    db = database(connected=False)

    assert await evaluate_text("hello", surface=SURFACE_CHAT_MESSAGE) is NOT_FLAGGED
    assert (db.opened, len(db.statements)) == (1, 1)

    for _ in range(5):
        assert await evaluate_text("hello again", surface=SURFACE_CHAT_MESSAGE) is NOT_FLAGGED
    assert (db.opened, len(db.statements)) == (1, 1)


async def test_the_none_connected_answer_is_read_again_once_it_expires(database, monkeypatch):
    db = database(connected=False)
    monkeypatch.setattr(prompt_guards, "CONNECTION_STATE_TTL_SECONDS", 0.0)

    await evaluate_text("hello", surface=SURFACE_CHAT_MESSAGE)
    await evaluate_text("hello", surface=SURFACE_CHAT_MESSAGE)
    assert db.opened == 2


async def test_empty_text_is_never_evaluated(database):
    db = database(connected=True, rules=[_rule_row("block")])

    assert await evaluate_text("", surface=SURFACE_CHAT_MESSAGE) is NOT_FLAGGED
    assert db.opened == 0


async def test_a_connected_guard_evaluates_on_the_one_session_it_opened(database):
    db = database(connected=True, rules=[_rule_row("log"), _rule_row("block")])

    verdict = await evaluate_text(f"please run {_CANARY} now", surface=SURFACE_CHAT_MESSAGE, user_id="u-1")

    assert verdict.flagged and verdict.blocked
    assert verdict.rule_action == "block"
    assert verdict.rules == ["Canary_log", "Canary_block"]
    assert verdict.categories == ["Injection"]
    assert verdict.source == "nova_guard"
    # The connection-state query and the rule query, on one session.
    assert (db.opened, len(db.statements)) == (1, 2)


async def test_the_evaluation_hands_its_connection_back_after_every_statement(database):
    db = database(connected=True, rules=[_rule_row("block")])
    events: list[str] = []
    real_execute, real_commit = db.execute, db.commit

    async def _execute(statement, *args, **kwargs):
        events.append("statement")
        return await real_execute(statement, *args, **kwargs)

    async def _commit():
        events.append("commit")
        await real_commit()

    db.execute, db.commit = _execute, _commit

    assert (await evaluate_text(_CANARY, surface=SURFACE_CHAT_MESSAGE)).blocked
    # No statement leaves its transaction open behind it: the engine holds no
    # pooled connection while it waits on an embedding or a model.
    assert events == ["statement", "commit", "statement", "commit"]


async def test_a_message_no_rule_matches_is_not_flagged(database):
    database(connected=True, rules=[_rule_row("block")])

    assert await evaluate_text("the team ships on Thursdays", surface=SURFACE_CHAT_MESSAGE) is NOT_FLAGGED


async def test_disconnecting_the_guard_applies_on_the_next_evaluation(database):
    db = database(connected=True, rules=[_rule_row("block")])
    assert (await evaluate_text(_CANARY, surface=SURFACE_CHAT_MESSAGE)).blocked

    db.connected = False
    assert await evaluate_text(_CANARY, surface=SURFACE_CHAT_MESSAGE) is NOT_FLAGGED
    assert db.opened == 2


async def test_a_guard_that_fails_is_skipped_and_logged(database, caplog):
    db = database(connected=True, rules=[_rule_row("block")])
    real_execute = db.execute

    async def _rules_unreadable(statement, *args, **kwargs):
        if db.statements:
            raise RuntimeError("rules table unreadable")
        return await real_execute(statement, *args, **kwargs)

    db.execute = _rules_unreadable

    assert await evaluate_text(_CANARY, surface=SURFACE_CHAT_MESSAGE) is NOT_FLAGGED
    assert "could not evaluate a chat_message" in caplog.text
    # A failure is not "none connected": the next evaluation reads again.
    await evaluate_text(_CANARY, surface=SURFACE_CHAT_MESSAGE)
    assert db.opened == 2


async def test_a_stalled_guard_is_cut_at_the_bound(database, caplog):
    db = database(connected=True, rules=[_rule_row("block")])

    async def _stall(statement, *args, **kwargs):
        await asyncio.sleep(5)

    db.execute = _stall
    started = time.monotonic()

    assert await evaluate_text(_CANARY, surface=SURFACE_CHAT_MESSAGE, timeout=0.05) is NOT_FLAGGED
    assert time.monotonic() - started < 1.0
    assert "could not evaluate a chat_message" in caplog.text


async def test_the_default_bound_is_read_when_the_evaluation_starts(database, monkeypatch):
    db = database(connected=True, rules=[_rule_row("block")])

    async def _stall(statement, *args, **kwargs):
        await asyncio.sleep(5)

    db.execute = _stall
    monkeypatch.setattr(prompt_guards, "GUARD_EVALUATION_TIMEOUT_SECS", 0.05)
    started = time.monotonic()

    assert await evaluate_text(_CANARY, surface=SURFACE_CHAT_MESSAGE) is NOT_FLAGGED
    assert time.monotonic() - started < 1.0


# ---------------------------------------------------------------------------
# The chat message: agent chat's audit and refusal
# ---------------------------------------------------------------------------


def _request_session() -> MagicMock:
    """The request's session: anything but the audit reaching it fails the test."""
    session = MagicMock()
    session.execute = AsyncMock(side_effect=AssertionError("the evaluation used the request's session"))
    return session


@pytest.fixture
def audit(monkeypatch) -> AsyncMock:
    recorder = AsyncMock(return_value=None)
    monkeypatch.setattr("app.services.audit.log_security_event", recorder)
    return recorder


async def _guard(session, message: str) -> str | None:
    return await guard_chat_message(
        session,
        message,
        user_id="user-1",
        entity_id="agent-id",
        entity_name="Release Desk",
        details={"agent_id": "agent-id", "agent_slug": "release-desk", "conversation_id": "conv-1"},
        anchor_agent_id="agent-id",
    )


async def test_a_block_rule_refuses_the_message_and_is_audited_on_the_request_session(database, audit):
    db = database(connected=True, rules=[_rule_row("log"), _rule_row("block")])
    request = _request_session()

    assert await _guard(request, f"do {_CANARY}") == NOVA_REFUSAL_MESSAGE

    audit.assert_awaited_once()
    assert audit.await_args.args == (request,)
    assert audit.await_args.kwargs == {
        "user_id": "user-1",
        "system_actor": "nova_guard",
        "anchor_agent_id": "agent-id",
        "action": "security_alert",
        "entity_type": "security",
        "entity_id": "agent-id",
        "entity_name": "Release Desk",
        "summary": "Nova rule match",
        "details": {
            "source": "nova_guard",
            "rules": ["Canary_log", "Canary_block"],
            "categories": ["Injection"],
            "agent_id": "agent-id",
            "agent_slug": "release-desk",
            "conversation_id": "conv-1",
            "action": "block",
            "blocked": True,
        },
    }
    # The evaluation ran on the session the guard opened, not the request's.
    assert db.opened == 1 and request not in db.sessions
    request.execute.assert_not_awaited()


async def test_a_log_rule_is_audited_and_the_message_continues(database, audit):
    database(connected=True, rules=[_rule_row("log")])

    assert await _guard(_request_session(), f"do {_CANARY}") is None

    details = audit.await_args.kwargs["details"]
    assert (details["action"], details["blocked"]) == ("log", False)


async def test_nothing_matched_audits_nothing(database, audit):
    database(connected=True, rules=[_rule_row("block")])

    assert await _guard(_request_session(), "the team ships on Thursdays") is None
    audit.assert_not_awaited()


async def test_with_no_guard_connected_the_chat_message_costs_nothing_but_the_cached_read(database, audit):
    db = database(connected=False)
    request = _request_session()

    for _ in range(3):
        assert await _guard(request, f"do {_CANARY}") is None
    assert (db.opened, len(db.statements)) == (1, 1)
    audit.assert_not_awaited()
    assert request.mock_calls == []
