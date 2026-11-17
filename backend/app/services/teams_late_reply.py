# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""A Teams chat reply that lands after a newer message quotes the message it answers.

A turn answering an earlier message of a chat (1:1 or group) can still be
running when someone writes again. Posted bare, its answer then sits under the
newer message and reads as the answer to it. This module decides, from the
chat's latest messages as Graph lists them, whether the reply would land after
a newer message from someone else, and builds the Graph ``messageReference``
attachment that shows the answered message quoted above the reply (Teams'
own "reply with quote"). A reply that is still the latest answer is posted as
before, without a quote; nothing else is ever posted.

Used by ``send_microsoft_chat_message`` (a run bound to a chat turn by
``chat_reply_guard``) and by the Teams agent-account reply
(``MicrosoftTeamsProvider.send_reply``). A channel thread reply already sits
under its root and is left as it is.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

#: Latest messages of the chat read to tell whether a newer one came in.
RECENT_MESSAGES = 20
#: Characters of the answered message shown in the quote.
PREVIEW_CHARS = 200

_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")


def _created(msg: dict[str, Any]) -> datetime:
    raw = str(msg.get("createdDateTime") or "")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _author(msg: dict[str, Any]) -> str:
    user = (msg.get("from") or {}).get("user") or {}
    return str(user.get("id") or "")


def _is_post(msg: dict[str, Any]) -> bool:
    return msg.get("messageType", "message") == "message" and not msg.get("deletedDateTime")


def message_to_quote(
    messages: Iterable[Any], answered_ids: Iterable[str], *, account_id: str | None = None
) -> dict[str, Any] | None:
    """The answered message to quote, when a newer message from someone else follows it.

    *messages* are the chat's latest messages in any order, *answered_ids*
    the inbound ids the reply answers (the newest of them listed is quoted).
    A newer post counts when its author is a person other than the account
    (*account_id*); without *account_id*, only a newer post of the answered
    message's own author counts, which is never the account. ``None``: the
    reply is still the latest answer, posted without a quote.
    """
    wanted = {str(i) for i in answered_ids if i}
    posts = [m for m in messages if isinstance(m, dict) and _is_post(m)]
    answered = [m for m in posts if str(m.get("id") or "") in wanted]
    if not answered:
        return None
    target = max(answered, key=_created)
    since = _created(target)
    sender = _author(target)
    for msg in posts:
        if str(msg.get("id") or "") in wanted or _created(msg) <= since:
            continue
        author = _author(msg)
        if not author:
            continue
        if account_id:
            if author != account_id:
                return target
        elif author == sender:
            return target
    return None


def _preview(msg: dict[str, Any]) -> str:
    content = str((msg.get("body") or {}).get("content") or "")
    text = _SPACES.sub(" ", html.unescape(_TAG.sub(" ", content))).strip()
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1].rstrip() + "\u2026"


def quote_attachment(msg: dict[str, Any]) -> dict[str, str]:
    """The Graph ``messageReference`` attachment that quotes *msg* above a reply."""
    message_id = str(msg.get("id") or "")
    sender = (msg.get("from") or {}).get("user") or {}
    content: dict[str, Any] = {"messageId": message_id, "messagePreview": _preview(msg)}
    if sender.get("id"):
        content["messageSender"] = {
            "user": {
                "id": str(sender.get("id")),
                "displayName": str(sender.get("displayName") or ""),
                "userIdentityType": str(sender.get("userIdentityType") or "aadUser"),
            }
        }
    return {"id": message_id, "contentType": "messageReference", "content": json.dumps(content)}


def with_quote(payload: dict[str, Any], msg: dict[str, Any]) -> dict[str, Any]:
    """*payload* (a Graph chatMessage) with *msg* quoted at the top of its body."""
    attachment = quote_attachment(msg)
    body = dict(payload.get("body") or {})
    body["contentType"] = "html"
    body["content"] = f'<attachment id="{attachment["id"]}"></attachment>' + str(body.get("content") or "")
    return {**payload, "body": body, "attachments": [attachment, *(payload.get("attachments") or [])]}
