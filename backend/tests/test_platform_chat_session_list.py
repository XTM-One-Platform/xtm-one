"""The embedded chat's history (``GET /api/v1/platform/chat/sessions``) - ``uv run pytest``.

Each row carries its last message (``last_message_preview``), as the web
chat's list writes it (``chat.last_message_previews``), and the channel the
conversation was started in: the panel shows the preview under the title and
sets it on a send, so a list read without it took it away again. PostgreSQL
runs the query itself in ``test_platform_chat_workspaces_postgres.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.api.v1 import platform as platform_api


class _Db:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _stmt):
        rows = self.rows
        return SimpleNamespace(all=lambda: list(rows))


async def test_each_row_carries_its_last_message_and_channel(monkeypatch):
    with_preview = SimpleNamespace(
        id=uuid.uuid4(),
        title="Triage",
        updated_at=datetime(2026, 11, 2, tzinfo=UTC),
        agent_id=None,
        workspace_id=None,
        channel=platform_api.PLATFORM_CHAT_CHANNEL,
    )
    without = SimpleNamespace(**{**vars(with_preview), "id": uuid.uuid4()})
    asked: list[list[uuid.UUID]] = []

    async def _previews(_db, conv_ids):
        asked.append(conv_ids)
        return {with_preview.id: "Check the hashes"}

    monkeypatch.setattr(platform_api, "last_message_previews", _previews)

    listed = await platform_api.list_platform_chat_sessions(
        db=_Db([(with_preview, 2, None), (without, 1, None)]), user=SimpleNamespace(id=uuid.uuid4())
    )

    assert asked == [[with_preview.id, without.id]]
    assert [(c["last_message_preview"], c["channel"]) for c in listed["conversations"]] == [
        ("Check the hashes", platform_api.PLATFORM_CHAT_CHANNEL),
        (None, platform_api.PLATFORM_CHAT_CHANNEL),
    ]
