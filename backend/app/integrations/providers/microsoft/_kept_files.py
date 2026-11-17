"""File cards a Teams message edit keeps.

Graph replaces a chatMessage whole on ``PATCH``: its body and its
``attachments``. A file posted as a ``reference`` attachment (the file card of
``send_microsoft_chat_file`` / ``send_microsoft_channel_file``) shows only
while the body carries its ``<attachment id="...">`` tag and the attachment is
sent again, so an edit that sends the new text alone drops the file from the
message. The edit tools read the message first and send its file attachments
back with the new body; a card or a quoted message is not kept (the edit's own
``card`` replaces the card, as before).
"""

from __future__ import annotations

from typing import Any

REFERENCE_CONTENT_TYPE = "reference"


def file_attachments(message: Any) -> list[dict]:
    """The ``reference`` (file) attachments of the chatMessage *message*, as Graph accepts them back."""
    attachments = message.get("attachments") if isinstance(message, dict) else None
    kept: list[dict] = []
    for item in attachments or ():
        if not isinstance(item, dict) or item.get("contentType") != REFERENCE_CONTENT_TYPE:
            continue
        attachment_id = str(item.get("id") or "")
        content_url = str(item.get("contentUrl") or "")
        if not attachment_id or not content_url:
            continue
        kept.append(
            {
                "id": attachment_id,
                "contentType": REFERENCE_CONTENT_TYPE,
                "contentUrl": content_url,
                "name": str(item.get("name") or "file"),
            }
        )
    return kept


def keep_file_attachments(payload: dict, files: list[dict]) -> None:
    """Add *files* to the edit *payload*: each attachment, and its tag at the end of the body."""
    if not files:
        return
    body = payload["body"]
    content = str(body.get("content") or "")
    for item in files:
        tag = f'<attachment id="{item["id"]}"></attachment>'
        if tag not in content:
            content += tag
    body["content"] = content
    body["contentType"] = "html"
    payload["attachments"] = [*(payload.get("attachments") or []), *files]


def kept_files_note(files: list[dict], read_error: str | None) -> str:
    """What an edit adds about the files of the message it edited."""
    if read_error:
        return (
            " Warning: the message could not be read before the edit, so a file it carried may no longer show"
            f" ({read_error.removeprefix('Error: ')})."
        )
    if not files:
        return ""
    return " Files kept: " + ", ".join(item["name"] for item in files) + "."
