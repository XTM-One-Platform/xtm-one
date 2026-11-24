"""``[[FILE:id]]`` markers in a Teams chat message are sent as files, never as text.

A model writes a conversation file into its answer as a ``[[FILE:<uuid>]]``
marker; the platform's own chat renders it as a file card, Teams does not: sent
as is through ``send_microsoft_chat_message`` it reaches the chat as a bare
token (#1829). The chat send is wrapped: the markers are taken out of the text,
the text (when anything is left, or a mention or a card is asked) is posted as
before, then each marked file goes through ``send_microsoft_chat_file``, the
upload that Teams shows as a file card. The answer names every file sent and
every one that failed, so a marker is never reported as delivered when it was
not.
"""

from __future__ import annotations

import re
from typing import Any

from app.tools.base import CallableToolExecutor, FileContext, ToolResult

#: A conversation-file marker; the id is a UUID, matched case-insensitively.
FILE_MARKER_RE = re.compile(
    r"\[\[FILE:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\]\]",
    re.IGNORECASE,
)

CHAT_SEND = "send_microsoft_chat_message"
CHAT_FILE = "send_microsoft_chat_file"


def split_file_markers(message: str) -> tuple[str, list[str]]:
    """The *message* without its file markers, and the distinct file ids in order."""
    ids: list[str] = []
    for file_id in FILE_MARKER_RE.findall(message or ""):
        if file_id.lower() not in (seen.lower() for seen in ids):
            ids.append(file_id)
    text = FILE_MARKER_RE.sub("", message or "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, ids


def _text(result: Any) -> tuple[str, bool]:
    if isinstance(result, ToolResult):
        return result.content, bool(result.is_error)
    text = str(result)
    return text, text.startswith(("Error", "Refused"))


def wrap_chat_send_file_markers(executor: CallableToolExecutor) -> None:
    """Make the chat send deliver ``[[FILE:id]]`` markers as Teams files."""
    send = executor.handler(CHAT_SEND)
    send_file = executor.handler(CHAT_FILE)
    if send is None or send_file is None:
        return

    async def send_with_files(
        chat_id: str,
        message: str = "",
        content_type: str = "text",
        mentions: list | str | None = None,
        card: dict | str | None = None,
        file_context: FileContext | None = None,
    ) -> Any:
        text, file_ids = split_file_markers(message if isinstance(message, str) else "")
        if not file_ids:
            return await send(chat_id=chat_id, message=message, content_type=content_type, mentions=mentions, card=card)
        parts: list[str] = []
        if text or mentions or card:
            result = await send(chat_id=chat_id, message=text, content_type=content_type, mentions=mentions, card=card)
            content, failed = _text(result)
            if failed:
                return result
            parts.append(content)
        sent: list[str] = []
        failures: list[str] = []
        for file_id in file_ids:
            result = await send_file(chat_id=chat_id, file_id=file_id, file_context=file_context)
            content, failed = _text(result)
            (failures if failed else sent).append(f"{file_id}: {content}")
        if sent:
            parts.append(f"{len(sent)} file marker(s) sent as Teams files:\n" + "\n".join(f"- {s}" for s in sent))
        if failures:
            parts.append(f"{len(failures)} file marker(s) NOT sent:\n" + "\n".join(f"- {f}" for f in failures))
        content = "\n".join(parts)
        if failures:
            return ToolResult(tool_call_id="", content=f"Error: not every file was sent.\n{content}", is_error=True)
        return content

    send_with_files.__doc__ = send.__doc__
    executor.register(CHAT_SEND, send_with_files)
