"""An OpenCTI playbook call to the platform chat exposes its bundle as ``$input``.

OpenCTI's AI-agent playbook components (``ai-agent-shared.ts``) post
``{agent_slug, content, stream: false}`` to ``/api/v1/platform/chat/messages``,
``content`` being the node's instructions, a ``--- STIX BUNDLE ---`` line and
the bundle as indented JSON. The route must hand that text to the agent run
unchanged, and the run must then expose the bundle as ``$input`` whatever the
agent's config says (unless it opts out with ``input_reference: false``).
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.api.v1 import platform as platform_api
from app.tools.session_input import input_reference_for_run, parse_input_document

_BUNDLE = {
    "type": "bundle",
    "id": "bundle--5d0092c5-5f74-4287-9642-33f4c354e56d",
    "objects": [
        {
            "type": "identity",
            "spec_version": "2.1",
            "id": "identity--f431f809-377b-45e0-aa1c-6a4751cae5ff",
            "name": "Example",
        }
    ],
}
_CONTENT = "Add the label phishing.\n\n--- STIX BUNDLE ---\n" + json.dumps(_BUNDLE, indent=2)


@pytest.mark.asyncio
async def test_a_playbook_shaped_call_reaches_the_run_with_its_bundle(monkeypatch):
    received: dict = {}

    async def _resolve(_db, _slug):
        return SimpleNamespace(id=uuid.uuid4(), name="Transformer", config={})

    async def _store_message(_db, _conv_id, role, content, channel, metadata_json=None):
        return SimpleNamespace(id=uuid.uuid4(), metadata_json=metadata_json, role=role)

    async def _history(_db, _conv_id):
        return []

    async def _quota(_db, _user_id):
        return None

    async def _run_agent_chat(_db, _uid, agent, _history, content, conversation_id, **kwargs):
        received["content"] = content
        received["config"] = agent.config
        return (json.dumps(_BUNDLE), None)

    async def _collect(_db, _ctx, _initial, **_kwargs):
        return ([], [])

    async def _link(_db, attachment_rows, *, conversation_id, message_id):
        return attachment_rows

    monkeypatch.setattr(platform_api, "_resolve_platform_agent", _resolve)
    monkeypatch.setattr("app.channels.router.store_message", _store_message)
    monkeypatch.setattr("app.channels.router.get_conversation_history", _history)
    monkeypatch.setattr("app.services.quota.check_agentic_quota", _quota)
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _run_agent_chat)
    monkeypatch.setattr(platform_api, "_collect_generated_attachments", _collect)
    monkeypatch.setattr(platform_api, "_link_generated_attachments", _link)

    conversations: list = []

    class _DB:
        def add(self, obj):
            conversations.append(obj)

        async def flush(self):
            for obj in conversations:
                if getattr(obj, "id", None) is None:
                    obj.id = uuid.uuid4()

        async def commit(self):
            return None

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/platform/chat/messages",
            "headers": [(b"x-platform-product", b"opencti")],
            "query_string": b"",
        }
    )
    await platform_api.send_platform_chat_message(
        platform_api.PlatformChatRequest(content=_CONTENT, agent_slug="cti-stix-transformer", stream=False),
        request=request,
        db=_DB(),  # type: ignore[arg-type]
        user=SimpleNamespace(id=uuid.uuid4(), email="u@example.com"),  # type: ignore[arg-type]
    )

    assert parse_input_document(received["content"]) == _BUNDLE
    # No opt-in on the agent: the bundle in the message is enough.
    assert input_reference_for_run(received["config"], received["content"]) is True
