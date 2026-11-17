"""flag_microsoft_email / unflag_microsoft_email read the flag back.

Microsoft Graph can accept a PATCH on a message id form that does not address
the message the mailbox lists (a short id taken from another account's change
notification) and leave the flag unchanged. The tools used to answer success
on the PATCH alone; they now read the flag back and answer an error when the
write did not land.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

_SHORT_FORM_ID = "AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0A" + "x" * 40
_MAILBOX_ID = "AAMkAGI2NGVhZTVlLTI1OGMtNDI4My1iZmVmLTI1ZmM5OWM5MTIwNgBGAAAAAADUuTJK1K9aTpCdqXop_4NaBwCd9nJ-tVysQos2hTfspaWSAAAAAAEMAAA="


class _Resp:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


class _Graph:
    """A fake Graph whose PATCH succeeds and whose stored flag is configurable."""

    def __init__(self, *, stored_status: str, patched_id: str | None = None) -> None:
        self.stored_status = stored_status
        self.patched_id = patched_id
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, _client, method: str, url: str, _provider: str, _op: str, **_kw: Any):
        self.calls.append((method, url))
        if method == "patch":
            return _Resp({"id": self.patched_id} if self.patched_id else {}), None
        return _Resp({"id": url.rsplit("/", 1)[-1], "flag": {"flagStatus": self.stored_status}}), None


async def _run(monkeypatch, graph: _Graph, tool: str, message_id: str):
    monkeypatch.setattr(ms._common, "safe_request", graph)
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    return await executor.execute_tool(ToolCall(id="c1", name=tool, arguments={"message_id": message_id}))


@pytest.mark.asyncio
async def test_flag_on_an_id_that_does_not_land_is_an_error(monkeypatch) -> None:
    """The reproduction: PATCH accepted, the message still reads notFlagged."""
    graph = _Graph(stored_status="notFlagged")
    result = await _run(monkeypatch, graph, "flag_microsoft_email", _SHORT_FORM_ID)

    assert [m for m, _ in graph.calls] == ["patch", "get"]
    assert result.is_error, result.content
    content = str(result.content)
    assert "flagged for follow-up" not in content
    assert "notFlagged" in content
    assert "search_microsoft_emails" in content


@pytest.mark.asyncio
async def test_flag_that_lands_is_confirmed_on_the_mailbox_id(monkeypatch) -> None:
    graph = _Graph(stored_status="flagged", patched_id=_MAILBOX_ID)
    result = await _run(monkeypatch, graph, "flag_microsoft_email", _SHORT_FORM_ID)

    assert not result.is_error, result.content
    # The read-back goes to the id Graph answered for the patched message.
    assert graph.calls[1][1].endswith(_MAILBOX_ID)
    assert _MAILBOX_ID in str(result.content)
    assert "read back: flagged" in str(result.content)


@pytest.mark.asyncio
async def test_unflag_that_does_not_land_is_an_error(monkeypatch) -> None:
    graph = _Graph(stored_status="flagged")
    result = await _run(monkeypatch, graph, "unflag_microsoft_email", _MAILBOX_ID)

    assert result.is_error, result.content
    assert "unflagged" not in str(result.content)


@pytest.mark.asyncio
async def test_unflag_that_lands_is_confirmed(monkeypatch) -> None:
    graph = _Graph(stored_status="notFlagged", patched_id=_MAILBOX_ID)
    result = await _run(monkeypatch, graph, "unflag_microsoft_email", _MAILBOX_ID)

    assert not result.is_error, result.content
    assert "read back: notFlagged" in str(result.content)
