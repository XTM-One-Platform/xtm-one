"""The prompt guard on the General Assistant and the public widget.

Both surfaces run the chat-message check agent chat runs, through the same
helper. Neither was guarded before, so by default a match is audited and the
message continues, a ``block`` rule included; the security options opt each
surface in to refusing. A widget visitor has no account: the match is audited
as the guard, anchored on the admin the widget acts as.

The evaluation is the real one (connection-state query, Nova engine, keyword
tier) over the counting stand-in of ``tests/test_prompt_guards.py``; the audit
is the real ``log_system_audit`` for the accountless case.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import security_options
from app.models.audit_log import AuditLog
from app.services import nova_engine
from app.services.nova_guard import NOVA_REFUSAL_MESSAGE
from app.services.prompt_guards import SURFACE_CHAT_MESSAGE, guard_chat_message
from tests.test_prompt_guards import _CANARY, _Database, _rule_row

pytestmark = pytest.mark.asyncio

_DETAILS = {"surface": SURFACE_CHAT_MESSAGE, "chat_surface": "public_widget", "conversation_id": "conv-1"}
_AGENT_ID = uuid.uuid4()


@pytest.fixture
def guard(monkeypatch):
    db = _Database(connected=True, rules=[_rule_row("log"), _rule_row("block")])
    monkeypatch.setattr("app.core.database.async_session", db)
    monkeypatch.setattr(nova_engine, "NOVA_HUNTING_AVAILABLE", False)
    return db


@pytest.fixture
def audit(monkeypatch) -> AsyncMock:
    recorder = AsyncMock(return_value=None)
    monkeypatch.setattr("app.services.audit.log_security_event", recorder)
    return recorder


async def _check(session, message: str = f"please {_CANARY}", **kwargs) -> str | None:
    kwargs.setdefault("user_id", "user-1")
    return await guard_chat_message(
        session, message, entity_id=_AGENT_ID, entity_name="Release Desk", details=_DETAILS, **kwargs
    )


async def test_a_surface_that_only_logs_audits_a_block_rule_and_lets_the_message_through(guard, audit):
    request = MagicMock()

    assert await _check(request, enforce=False) is None

    audit.assert_awaited_once()
    assert audit.await_args.args == (request,)
    assert audit.await_args.kwargs["details"] == {
        "source": "nova_guard",
        "rules": ["Canary_log", "Canary_block"],
        "categories": ["Injection"],
        **_DETAILS,
        "rule_action": "block",
        "action": "allowed",
        "blocked": False,
    }


async def test_a_surface_that_enforces_refuses_a_block_rule(guard, audit):
    assert await _check(MagicMock(), enforce=True) == NOVA_REFUSAL_MESSAGE

    details = audit.await_args.kwargs["details"]
    assert (details["action"], details["blocked"]) == ("block", True)
    assert "rule_action" not in details


async def test_an_enforcing_surface_lets_a_log_rule_through(guard, audit):
    guard.rules = [_rule_row("log")]

    assert await _check(MagicMock(), enforce=True) is None
    details = audit.await_args.kwargs["details"]
    assert (details["action"], details["blocked"]) == ("log", False)


async def test_a_visitor_match_is_audited_as_the_guard_anchored_on_the_admin(guard, audit):
    request = MagicMock()
    request.flush = AsyncMock()
    admin_id = str(uuid.uuid4())

    assert await _check(request, user_id=None, enforce=True, audit_anchor_user_id=admin_id) == NOVA_REFUSAL_MESSAGE

    audit.assert_not_awaited()
    entry = request.add.call_args.args[0]
    assert isinstance(entry, AuditLog)
    assert (entry.user_id, entry.user_email, entry.action, entry.entity_type) == (
        uuid.UUID(admin_id),
        "system:nova_guard",
        "security_alert",
        "security",
    )
    assert (entry.entity_id, entry.entity_name, entry.summary) == (_AGENT_ID, "Release Desk", "Nova rule match")
    assert entry.details == {
        "source": "nova_guard",
        "rules": ["Canary_log", "Canary_block"],
        "categories": ["Injection"],
        **_DETAILS,
        "action": "block",
        "blocked": True,
        "actor": "system:nova_guard",
    }
    request.flush.assert_awaited_once()


async def test_a_visitor_match_with_nobody_to_anchor_it_on_is_logged(guard, audit, caplog):
    request = MagicMock()

    assert await _check(request, user_id=None, enforce=False) is None
    audit.assert_not_awaited()
    request.add.assert_not_called()
    assert "with no user to audit it under" in caplog.text


class _AuditSession:
    """The audit's session: the guard's connection state, then the embedded widget count."""

    def __init__(self, *, connected: bool, widgets: int) -> None:
        self._connected = connected
        self._widgets = widgets

    async def execute(self, statement):
        row = (uuid.uuid4(),) if self._connected else None
        widgets = self._widgets
        return SimpleNamespace(first=lambda: row, scalar=lambda: widgets)


async def _audit_findings(**kwargs) -> dict[str, object]:
    from app.api.v1.security_audit import _check_prompt_guards

    findings: list = []
    await _check_prompt_guards(_AuditSession(**kwargs), findings)
    return {finding.id: finding for finding in findings}


async def test_the_security_audit_says_nothing_while_no_guard_is_connected():
    assert await _audit_findings(connected=False, widgets=3) == {}


async def test_the_security_audit_says_blocking_is_off_on_both_new_surfaces_by_default():
    findings = await _audit_findings(connected=True, widgets=2)

    assert set(findings) == {
        "agents.prompt_guard_general_assistant_logs_only",
        "agents.prompt_guard_public_widget_logs_only",
    }
    general = findings["agents.prompt_guard_general_assistant_logs_only"]
    assert (general.severity, general.control) == ("info", "agents.prompt_guard_blocks_general_assistant")
    assert "Blocking is off on the General Assistant" in general.description
    assert "PROMPT_GUARD_BLOCK_GENERAL_ASSISTANT=true" in general.recommendation
    widget = findings["agents.prompt_guard_public_widget_logs_only"]
    assert (widget.severity, widget.control) == ("info", "agents.prompt_guard_blocks_public_widget")
    assert "PROMPT_GUARD_BLOCK_PUBLIC_WIDGET=true" in widget.recommendation


async def test_the_security_audit_passes_the_surfaces_that_block_and_skips_a_widget_nobody_embeds():
    security_options.publish_stored(
        {"prompt_guard_block_general_assistant": True, "prompt_guard_block_public_widget": True}
    )
    findings = await _audit_findings(connected=True, widgets=1)
    assert {fid: f.severity for fid, f in findings.items()} == {
        "agents.prompt_guard_general_assistant_blocks": "pass",
        "agents.prompt_guard_public_widget_blocks": "pass",
    }

    assert set(await _audit_findings(connected=True, widgets=0)) == {"agents.prompt_guard_general_assistant_blocks"}


async def test_nothing_matched_touches_no_session_of_the_request(guard, audit):
    request = MagicMock()

    assert await _check(request, "the team ships on Thursdays", enforce=True) is None
    assert request.mock_calls == []
    audit.assert_not_awaited()
