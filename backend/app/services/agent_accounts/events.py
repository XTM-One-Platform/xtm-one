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

"""What every provider hands the agent-account engine, whatever the platform.

A provider turns its own payloads into these shapes; the rule engine, the
pipeline and the API only ever read them, so a new platform is a provider
module and nothing else.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

#: A one-to-one chat, a chat of several people, a channel of a team / space. A
#: mailbox reads them as an email to the account alone, and one with other recipients.
CONVERSATION_TYPES = ("direct", "group", "channel")


@dataclass(frozen=True)
class AccountAttachment:
    """A file or an image a message carries, as its provider downloads it.

    Read from the message only: nothing is downloaded until the agent answers
    it (``AccountProvider.download_attachment``).
    """

    name: str
    #: Where the provider downloads it from, with the account's own credentials.
    url: str
    #: The media type the message declares, ``application/octet-stream`` when it
    #: declares none (the download's answer then says).
    content_type: str = "application/octet-stream"


@dataclass(frozen=True)
class AccountEvent:
    """One message the account received or wrote, normalized."""

    provider: str
    conversation_type: str
    #: The chat, or the channel, the message was posted in.
    conversation_id: str
    #: What a reply continues: the chat for a chat, the root message for a channel thread.
    thread_id: str
    message_id: str
    sender_id: str
    sender_name: str
    text: str
    created_at: datetime
    conversation_name: str | None = None
    sender_email: str | None = None
    #: Every address the provider knows the sender by (``sender_email`` first): a
    #: directory can hold a mail address and a sign-in name that differ.
    sender_emails: tuple[str, ...] = ()
    #: The account itself is @mentioned in the message.
    mentions_account: bool = False
    #: The account wrote it (by hand, or the agent through it).
    from_account: bool = False
    #: An application, a bot, a connector or a workflow wrote it.
    from_application: bool = False
    #: Another agent account wrote it, as the message itself says (a mailbox
    #: reads the header the agent accounts' emails carry).
    from_agent_account: bool = False
    #: The account's owner wrote it to the account itself (WhatsApp's "message
    #: yourself" chat): an instruction of the owner, whose echoes of the agent's
    #: own replies are told apart by message id (``state.is_agent_post``).
    self_chat: bool = False
    #: The people a reply also goes to besides the sender, as the sender chose
    #: them (a mailbox's reply-all to every other ``To`` and ``Cc``); 0
    #: when a reply reaches the sender, or the conversation, alone.
    reply_recipients: int = 0
    #: What the provider needs to post a reply into the conversation.
    route: dict[str, Any] = field(default_factory=dict)
    #: The files and pasted images the message carries.
    attachments: tuple[AccountAttachment, ...] = ()
    #: An email's RFC 5322 Message-ID, the same in every mailbox it reached:
    #: the key of the agent-wide claim that keeps an agent account and an
    #: assignment of the same agent from both answering it.
    internet_message_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AccountEvent:
        values = dict(data)
        created = values.get("created_at")
        if isinstance(created, str):
            parsed = datetime.fromisoformat(created)
            values["created_at"] = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        if isinstance(values.get("sender_emails"), list):
            values["sender_emails"] = tuple(values["sender_emails"])
        if isinstance(values.get("attachments"), (list, tuple)):
            values["attachments"] = tuple(
                item
                if isinstance(item, AccountAttachment)
                else AccountAttachment(**{k: v for k, v in item.items() if k in AccountAttachment.__dataclass_fields__})
                for item in values["attachments"]
                if isinstance(item, AccountAttachment)
                or (isinstance(item, dict) and item.get("name") and item.get("url"))
            )
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in values.items() if k in known})

    @property
    def sender_addresses(self) -> tuple[str, ...]:
        """Every address of the sender, the primary first, each once whatever its case."""
        seen: set[str] = set()
        out: list[str] = []
        for address in (self.sender_email, *self.sender_emails):
            if isinstance(address, str) and address.strip() and address.strip().lower() not in seen:
                seen.add(address.strip().lower())
                out.append(address.strip())
        return tuple(out)


@dataclass(frozen=True)
class ContextMessage:
    """A message of the conversation before the one being answered."""

    message_id: str
    sender_name: str
    text: str
    created_at: datetime
    from_account: bool = False


@dataclass(frozen=True)
class AccountIdentity:
    """Who the account is, as its provider names it."""

    external_id: str
    display_name: str
    email: str | None = None


@dataclass(frozen=True)
class ConversationOption:
    """A conversation a rule can target, for the editor's picker."""

    id: str
    name: str
    type: str
    #: The team / space a channel belongs to, which subscribing to it needs.
    team_id: str | None = None


@dataclass(frozen=True)
class PersonOption:
    """A person a rule can focus on or exclude, for the editor's picker."""

    id: str
    name: str
    email: str | None = None
