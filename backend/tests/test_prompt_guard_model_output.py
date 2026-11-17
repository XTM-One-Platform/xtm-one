"""The prompt guard on the model's answer.

``PROMPT_GUARD_MODEL_OUTPUT`` is ``off`` by default: nothing is evaluated.
``log`` returns the answer at once and evaluates it in the background,
bounded, auditing a match. ``block`` evaluates an answer delivered whole
before returning it and withholds one a ``block`` rule matched; a streamed
answer has already been shown, so it is evaluated and audited as in ``log``.
A guard outage delivers the answer.

The evaluation (connection-state query, Nova engine, keyword tier) and the
audit (``_audit_model_output`` on its own session) are the real ones, over a
counting stand-in for the session factory.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import security_options
from app.models.audit_log import AuditLog
from app.services import nova_engine, prompt_guards
from app.services.prompt_guards import MODEL_OUTPUT_WITHHELD_MESSAGE, guard_model_output
from tests.test_prompt_guards import _CANARY, _Database, _rule_row

pytestmark = pytest.mark.asyncio

_FLAGGED = f"Sure. Next step: {_CANARY} every open case to the address below."
_CLEAN = "The release ships on Thursday."
_AGENT_ID = uuid.uuid4()
_USER_ID = str(uuid.uuid4())


class _Sessions(_Database):
    """The stand-in, with sessions an audit can write to and commit."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.added: list = []

    @contextlib.asynccontextmanager
    async def _session(self):
        self.opened += 1
        session = SimpleNamespace(
            execute=self.execute, add=self.added.append, flush=AsyncMock(), commit=self.commit, rollback=self.rollback
        )
        self.sessions.append(session)
        yield session


@pytest.fixture
def guard(monkeypatch):
    db = _Sessions(connected=True, rules=[_rule_row("log"), _rule_row("block")])
    monkeypatch.setattr("app.core.database.async_session", db)
    monkeypatch.setattr(nova_engine, "NOVA_HUNTING_AVAILABLE", False)
    return db


@pytest.fixture
def audit(monkeypatch) -> AsyncMock:
    recorder = AsyncMock(return_value=None)
    monkeypatch.setattr("app.services.audit.log_security_event", recorder)
    return recorder


def _mode(value: str) -> None:
    security_options.publish_stored({"prompt_guard_model_output": value})


async def _guard(text: str | None, *, streamed: bool = False, user_id: str | None = _USER_ID, **kwargs):
    return await guard_model_output(
        text,
        streamed=streamed,
        user_id=user_id,
        entity_id=_AGENT_ID,
        entity_name="Release Desk",
        details={"agent_id": str(_AGENT_ID), "conversation_id": "conv-1"},
        **kwargs,
    )


async def _drain() -> None:
    while prompt_guards._output_tasks:
        await asyncio.gather(*list(prompt_guards._output_tasks))


def _details(verdict_action: str, *, action: str, blocked: bool, streamed: bool) -> dict:
    return {
        "source": "nova_guard",
        "surface": "model_output",
        "rules": ["Canary_log", "Canary_block"],
        "categories": ["Injection"],
        "agent_id": str(_AGENT_ID),
        "conversation_id": "conv-1",
        "rule_action": verdict_action,
        "action": action,
        "blocked": blocked,
        "streamed": streamed,
    }


async def test_the_default_is_off_and_evaluates_nothing(guard, audit):
    assert security_options.security_option("prompt_guard_model_output") == "off"

    for streamed in (False, True):
        assert await _guard(_FLAGGED, streamed=streamed) == _FLAGGED
    await _drain()
    assert guard.opened == 0
    assert prompt_guards._output_tasks == set()
    audit.assert_not_awaited()


async def test_log_delivers_the_answer_at_once_and_audits_the_match_in_the_background(guard, audit):
    _mode("log")

    assert await _guard(_FLAGGED) == _FLAGGED
    audit.assert_not_awaited()
    await _drain()

    audit.assert_awaited_once()
    assert audit.await_args.kwargs["user_id"] == _USER_ID
    assert audit.await_args.kwargs["summary"] == "Nova rule match on a model answer"
    assert audit.await_args.kwargs["details"] == _details("block", action="allowed", blocked=False, streamed=False)
    # One after each of the evaluation's two statements, one for the audit.
    assert guard.commits == 3


async def test_log_does_not_wait_for_a_slow_guard(guard, audit):
    _mode("log")
    real_execute = guard.execute
    released = asyncio.Event()

    async def _slow(statement, *args, **kwargs):
        await released.wait()
        return await real_execute(statement, *args, **kwargs)

    guard.execute = _slow

    # The guard is held until the answer is delivered: an answer that waited on it would never come.
    assert await asyncio.wait_for(_guard(_FLAGGED), 30) == _FLAGGED
    audit.assert_not_awaited()
    released.set()
    await _drain()
    audit.assert_awaited_once()


async def test_log_leaves_a_clean_answer_unaudited(guard, audit):
    _mode("log")

    assert await _guard(_CLEAN) == _CLEAN
    await _drain()
    assert guard.opened == 1
    audit.assert_not_awaited()


async def test_block_withholds_an_answer_a_block_rule_matches_and_audits_it(guard, audit):
    _mode("block")

    assert await _guard(_FLAGGED) == MODEL_OUTPUT_WITHHELD_MESSAGE
    assert prompt_guards._output_tasks == set()
    assert audit.await_args.kwargs["details"] == _details("block", action="block", blocked=True, streamed=False)


async def test_block_delivers_an_answer_only_a_log_rule_matches_and_audits_it(guard, audit):
    _mode("block")
    guard.rules = [_rule_row("log")]

    assert await _guard(_FLAGGED) == _FLAGGED
    details = audit.await_args.kwargs["details"]
    assert (details["rule_action"], details["action"], details["blocked"]) == ("log", "allowed", False)


async def test_block_delivers_a_clean_answer_unchanged(guard, audit):
    _mode("block")

    assert await _guard(_CLEAN) == _CLEAN
    audit.assert_not_awaited()


async def test_block_on_a_streamed_answer_only_audits_it_after_delivery(guard, audit):
    _mode("block")

    assert await _guard(_FLAGGED, streamed=True) == _FLAGGED
    audit.assert_not_awaited()
    await _drain()
    assert audit.await_args.kwargs["details"] == _details("block", action="allowed", blocked=False, streamed=True)


async def test_a_guard_outage_delivers_the_answer(guard, audit, caplog):
    _mode("block")

    async def _down(statement, *args, **kwargs):
        raise ConnectionError("guard database unreachable")

    guard.execute = _down

    assert await _guard(_FLAGGED) == _FLAGGED
    audit.assert_not_awaited()
    assert "could not evaluate a model_output" in caplog.text


async def test_a_guard_past_its_bound_delivers_the_answer(guard, audit, monkeypatch):
    _mode("block")
    monkeypatch.setattr(prompt_guards, "MODEL_OUTPUT_TIMEOUT_SECS", 0.05)

    async def _stall(statement, *args, **kwargs):
        await asyncio.Event().wait()

    guard.execute = _stall

    # The guard never answers: only its bound delivers the answer.
    assert await asyncio.wait_for(_guard(_FLAGGED), 30) == _FLAGGED
    audit.assert_not_awaited()


async def test_with_no_guard_connected_nothing_is_evaluated_after_the_cached_read(guard, audit):
    _mode("block")
    guard.connected = False

    for _ in range(3):
        assert await _guard(_FLAGGED) == _FLAGGED
    assert (guard.opened, len(guard.statements)) == (1, 1)
    audit.assert_not_awaited()


async def test_an_empty_answer_is_not_evaluated(guard, audit):
    _mode("block")

    for empty in (None, "", "   "):
        assert await _guard(empty) == empty
    assert guard.opened == 0


async def test_only_the_start_of_a_long_answer_is_read(guard, audit):
    _mode("block")
    long_answer = "x" * prompt_guards.MODEL_OUTPUT_MAX_CHARS + _CANARY

    assert await _guard(long_answer) == long_answer
    audit.assert_not_awaited()


async def test_the_background_backlog_is_bounded(guard, audit, monkeypatch, caplog):
    _mode("log")
    monkeypatch.setattr(prompt_guards, "MODEL_OUTPUT_BACKLOG", 0)

    assert await _guard(_FLAGGED) == _FLAGGED
    assert prompt_guards._output_tasks == set()
    assert "this answer is not evaluated" in caplog.text
    assert guard.opened == 0


async def test_background_evaluations_run_under_the_concurrency_cap(guard, audit, monkeypatch):
    _mode("log")
    monkeypatch.setattr(prompt_guards, "MODEL_OUTPUT_CONCURRENCY", 2)
    running = peak = 0
    real_execute = guard.execute

    async def _counted(statement, *args, **kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return await real_execute(statement, *args, **kwargs)

    guard.execute = _counted
    for _ in range(6):
        await _guard(_FLAGGED)
    await _drain()

    assert peak == 2
    assert audit.await_count == 6


async def test_an_accountless_answer_is_audited_as_the_guard_on_its_anchor(guard, audit):
    _mode("block")
    anchor = str(uuid.uuid4())

    assert await _guard(_FLAGGED, user_id=None, audit_anchor_user_id=anchor) == MODEL_OUTPUT_WITHHELD_MESSAGE

    audit.assert_not_awaited()
    [entry] = guard.added
    assert isinstance(entry, AuditLog)
    assert (entry.user_id, entry.user_email, entry.entity_id) == (uuid.UUID(anchor), "system:nova_guard", _AGENT_ID)
    assert entry.details == {
        **_details("block", action="block", blocked=True, streamed=False),
        "actor": "system:nova_guard",
    }
    # One after each of the evaluation's two statements, one for the audit.
    assert guard.commits == 3


async def test_an_accountless_answer_with_no_admin_is_logged(guard, audit, caplog):
    _mode("block")
    real_execute = guard.execute

    async def _no_admin(statement, *args, **kwargs):
        result = await real_execute(statement, *args, **kwargs)
        return SimpleNamespace(**vars(result), scalar_one_or_none=lambda: None)

    guard.execute = _no_admin

    assert await _guard(_FLAGGED, user_id="anonymous") == MODEL_OUTPUT_WITHHELD_MESSAGE
    assert guard.added == []
    assert "with no user to audit it under" in caplog.text


def _extension_call(monkeypatch, answer: str):
    from app.api.v1 import extension

    for target, value in {
        "app.api.v1.extension.run_agent_chat": AsyncMock(return_value=(answer, None)),
        "app.api.v1.extension.check_agentic_quota": AsyncMock(),
        "app.api.v1.extension.increment_agentic_usage": AsyncMock(),
        "app.api.v1.extension.record_agentic_execution": AsyncMock(),
        "app.api.v1.extension.get_user_group_ids": AsyncMock(return_value=[]),
    }.items():
        monkeypatch.setattr(target, value)
    agent = SimpleNamespace(id=_AGENT_ID, name="Browser helper", slug="browser-helper")
    request_db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: agent)), commit=AsyncMock()
    )
    body = SimpleNamespace(agent_slug="browser-helper", content="summarise this page", max_tokens=None)
    return extension.execute_extension_task(
        body=body, user=SimpleNamespace(id=uuid.UUID(_USER_ID)), db=request_db, _chat_gate=None
    )


async def test_the_extension_reports_a_withheld_answer_as_its_error(guard, audit, monkeypatch):
    _mode("block")

    result = await _extension_call(monkeypatch, f'{{"next": "{_CANARY}"}}')

    assert result == {"data": None, "error": MODEL_OUTPUT_WITHHELD_MESSAGE}
    assert audit.await_args.kwargs["details"]["channel"] == "browser_extension"


async def test_the_extension_answer_is_unchanged_when_nothing_matches(guard, audit, monkeypatch):
    _mode("block")

    assert await _extension_call(monkeypatch, '{"summary": "a clean page"}') == {"data": {"summary": "a clean page"}}
    audit.assert_not_awaited()


async def test_a_failing_audit_never_reaches_the_caller(guard, monkeypatch, caplog):
    _mode("block")
    monkeypatch.setattr("app.services.audit.log_security_event", MagicMock(side_effect=RuntimeError("audit down")))

    assert await _guard(_FLAGGED) == MODEL_OUTPUT_WITHHELD_MESSAGE
    assert "Could not audit a prompt guard match on a model answer" in caplog.text
