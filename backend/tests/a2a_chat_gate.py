"""Stub the chat gate for A2A tests that are not about it.

A2A is offered exactly while agentic chat is. Every A2A path resolves
``api.v1.platform.is_chat_enabled`` at call time — the routes via
``deps.require_chat_enabled``, the probes, the pre-flight, the sweep and the
tool wiring — so patching that one attribute is enough. The routes' dependency
asks the AI state first, so that is pinned on too; the gate itself is covered
in ``test_a2a_follows_chat.py`` and ``test_require_chat_enabled.py``.
"""

from __future__ import annotations


def stub_chat_gate(monkeypatch, enabled: bool = True) -> None:
    """Make every A2A chat-gate check answer ``enabled`` for this test."""

    async def _chat(_db, *, tier=None):
        return enabled

    async def _ai(_db):
        return True

    monkeypatch.setattr("app.api.v1.platform.is_chat_enabled", _chat)
    monkeypatch.setattr("app.services.ai_state.is_ai_enabled", _ai)
