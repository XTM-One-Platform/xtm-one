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

"""Real Teams mentions in an agent account's conversational reply.

The message tools take their mentions as data (``mentions``); a reply the agent
writes in a conversation only has its text. Before the reply is posted, each
``@`` token of the text is matched against the members of the chat or channel
it goes to:

- ``@email`` of a member;
- ``@Full Name`` as Teams shows it (the longest name first, any case);
- ``@Firstname`` when exactly one member has that first name.

A matched token becomes the ``<at id="N">`` marker
:func:`app.services.teams_format.build_graph_chat_body` turns into a Teams tag,
with its Graph ``mentions`` entry built as the tools build theirs. A token that
matches nobody, or several people, stays plain text: a reply never notifies
the wrong person. Code spans and code blocks are left as they are, and the
account itself is never mentioned.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable
from typing import Any

from app.integrations.providers.microsoft._mentions import Mention

#: Fenced code blocks and inline code spans: never scanned for mentions.
_CODE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.DOTALL)
#: An ``@`` that may open a mention: at the start or after a non-word
#: character (so an email address inside the text is not a token), followed by
#: a letter.
_AT = re.compile(r"(?<![\w@])@(?=[^\W\d_])")
#: A mention marker the text already holds (``teams_format`` reads the same shape).
_MARKER = re.compile(r"<at\b[^>]*?\bid\s*=", re.IGNORECASE)
#: The first word of an ``@Firstname`` token.
_WORD = re.compile(r"[^\W\d_][\w'-]*")


@dataclasses.dataclass(frozen=True)
class Member:
    """A member of the conversation a reply is posted in."""

    user_id: str
    display_name: str
    email: str = ""


@dataclasses.dataclass
class ReplyMentions:
    """The reply text with its mention markers, the mentions, and what stayed plain."""

    text: str
    mentions: list[Mention] = dataclasses.field(default_factory=list)
    unresolved: list[str] = dataclasses.field(default_factory=list)

    def report(self) -> dict[str, list[str]]:
        """What the delivery records: the names mentioned and the tokens left as text."""
        return {
            "mentions_resolved": [mention.display_name for mention in self.mentions],
            "mentions_unresolved": list(self.unresolved),
        }


def may_mention(text: str) -> bool:
    """Whether *text* holds an ``@`` token outside code: only then are the members read.

    A text that already places ``<at id="N">`` markers is left alone: those
    would point at mentions it does not carry.
    """
    if not text or _MARKER.search(text):
        return False
    return bool(_AT.search(_CODE.sub(" ", text)))


def members_of(raw: Iterable[dict[str, Any]], account_id: str) -> list[Member]:
    """The Graph ``conversationMember`` entries as members, the account itself left out."""
    members: list[Member] = []
    own = (account_id or "").lower()
    for entry in raw:
        user_id = str(entry.get("userId") or "").strip()
        name = " ".join(str(entry.get("displayName") or "").split())
        if not user_id or not name or user_id.lower() == own:
            continue
        members.append(Member(user_id, name, str(entry.get("email") or "").strip()))
    return members


def _ends_a_word(text: str, end: int) -> bool:
    return end >= len(text) or not (text[end].isalnum() or text[end] == "_")


class _Matcher:
    def __init__(self, members: list[Member]):
        self.emails: dict[str, list[Member]] = {}
        self.names: dict[str, list[Member]] = {}
        self.first: dict[str, list[Member]] = {}
        for member in members:
            if member.email:
                self.emails.setdefault(member.email.casefold(), []).append(member)
            self.names.setdefault(member.display_name.casefold(), []).append(member)
            self.first.setdefault(member.display_name.split()[0].casefold(), []).append(member)
        # Longest first: "@Jane Doe" is Jane Doe before it is a "@Jane".
        self.keys = sorted(
            [("email", key) for key in self.emails] + [("name", key) for key in self.names],
            key=lambda item: len(item[1]),
            reverse=True,
        )

    def match(self, text: str, start: int) -> tuple[int, Member | None, str]:
        """The end of the token at *start* (after the ``@``), its member, and its text."""
        folded = text[start:].casefold()
        for kind, key in self.keys:
            if folded.startswith(key) and _ends_a_word(text, start + len(key)):
                found = (self.emails if kind == "email" else self.names)[key]
                end = start + len(key)
                return end, found[0] if len(found) == 1 else None, text[start:end]
        word = _WORD.match(text, start)
        if not word:
            return start, None, ""
        found = self.first.get(word.group(0).casefold(), [])
        return word.end(), found[0] if len(found) == 1 else None, word.group(0)


def _scan(segment: str, matcher: _Matcher, result: ReplyMentions, parts: list[str]) -> None:
    position = 0
    for at in _AT.finditer(segment):
        if at.start() < position:
            continue
        end, member, token = matcher.match(segment, at.end())
        if not token:
            continue
        parts.append(segment[position : at.start()])
        if member is None:
            result.unresolved.append(f"@{token}")
            parts.append(segment[at.start() : end])
        else:
            # The name shown: the member's own for an email or a full name,
            # the first name as written for "@Firstname".
            shown = token if token.casefold() == member.display_name.split()[0].casefold() else member.display_name
            index = len(result.mentions)
            result.mentions.append(Mention("user", member.user_id, shown))
            parts.append(f'<at id="{index}"/>')
        position = end
    parts.append(segment[position:])


def resolve_reply_mentions(text: str, members: list[Member]) -> ReplyMentions:
    """*text* with each ``@`` token of a member as a mention marker; the others kept as written."""
    result = ReplyMentions(text=text)
    if not may_mention(text):
        return result
    if not members:
        result.unresolved = [f"@{word.group(0)}" for word in _tokens(text)]
        return result
    matcher = _Matcher(members)
    parts: list[str] = []
    position = 0
    for code in _CODE.finditer(text):
        _scan(text[position : code.start()], matcher, result, parts)
        parts.append(code.group(0))
        position = code.end()
    _scan(text[position:], matcher, result, parts)
    result.text = "".join(parts)
    return result


def _tokens(text: str) -> list[re.Match[str]]:
    """The first word of every ``@`` token outside code (for the report when nobody can match)."""
    stripped = _CODE.sub(" ", text)
    return [word for at in _AT.finditer(stripped) if (word := _WORD.match(stripped, at.end()))]
