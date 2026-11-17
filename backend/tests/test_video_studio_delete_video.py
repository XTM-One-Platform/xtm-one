"""``delete_video``: the tool's definition, who may delete, and a bad call answered without a database.

The deletion itself, against PostgreSQL, is ``test_video_studio_delete_video_postgres.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.services.video_studio import deletion


def test_the_tool_is_in_the_video_studio_suite_and_says_when_to_use_it():
    from app.services.builtin_tools.video_studio import build_video_studio_tools

    definitions, executor = build_video_studio_tools(user_id="")
    tool = next(d for d in definitions if d.name == "delete_video")
    assert tool.input_schema["required"] == ["video_id"]
    assert tool.input_schema["properties"]["confirm"]["type"] == "boolean"
    assert "ONLY when the user explicitly asks" in tool.description
    assert "no longer available" in tool.description
    assert "recordings and AI shots stay in the library" in tool.description
    assert "delete_video" in executor._handlers


def test_the_delete_is_a_write_gated_like_every_write():
    from app.services.hitl_whitelist_default import SHIPPED_DEFAULT_WHITELIST
    from app.tools import agent_loop as al

    assert al._is_write_tool("delete_video")
    assert "delete_video" not in SHIPPED_DEFAULT_WHITELIST


_OWNER = uuid.uuid4()


@pytest.mark.parametrize(
    ("company", "owner", "caller", "role", "allowed"),
    [
        (False, _OWNER, _OWNER, "user", True),
        (False, _OWNER, uuid.uuid4(), "user", False),
        # An administrator's agent never removes a colleague's personal video from a chat.
        (False, _OWNER, uuid.uuid4(), "admin", False),
        (True, _OWNER, _OWNER, "user", False),
        (True, _OWNER, uuid.uuid4(), "admin", True),
        (False, None, _OWNER, "user", False),
    ],
)
def test_only_the_owner_or_an_admin_on_a_company_video_may_delete(company, owner, caller, role, allowed):
    project = SimpleNamespace(is_company_managed=company, user_id=owner)
    user = SimpleNamespace(id=caller, role=role)
    assert deletion.agent_may_delete(project, user) is allowed


def test_a_caller_who_is_not_signed_in_deletes_nothing(monkeypatch):
    from app.services.builtin_tools import video_studio as tools
    from app.tools.base import ToolCall

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, *_a):
            return None

    monkeypatch.setattr(tools, "async_session", _Session)
    _definitions, executor = tools.build_video_studio_tools(user_id="")
    result = asyncio.run(
        executor.execute_tool(ToolCall(id="c", name="delete_video", arguments={"video_id": str(uuid.uuid4())}))
    )
    assert result.is_error and "you can open. Nothing was deleted." in result.content
