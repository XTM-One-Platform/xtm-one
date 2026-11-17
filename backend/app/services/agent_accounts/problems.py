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

"""What keeps an agent account from hearing its conversations, said so its owner can act.

A sync records one :class:`Problem` per step that failed - a subscription the
provider refused, a renewal, a poll, the account's integration - and stores
them on the account as its sync report (``AgentAccount.sync_report``): when it
ran, how many subscriptions it wanted and holds, and the problems grouped by
cause, each with the conversations it affects and the provider's own words for
the technical details.

The kind says what can be done. A provider reports the kinds it reads from
the status of its own answers (``providers.ERROR_KINDS``); the sync adds the
ones only it can see: another agent account or another notification address
holding the conversation, a provider that never called the notification
address back, an integration that cannot be used, a platform account another
agent account operates, a platform without a public HTTPS address, a
subscription the provider would only grant without the reactions, a
conversation the rules cover that the provider never subscribes to. The
frontend names every kind (``SYNC_PROBLEM_COPY``).

The technical details carry the provider's words as they are. A reason the
platform words itself travels as a note instead - a code and the values it
reads - that the frontend renders in the viewer's language
(``SYNC_PROBLEM_NOTES``); its English sentence stays in ``detail`` for
the logs and the account's last error.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services.agent_accounts.providers import DesiredSubscription, ProviderError

#: Every kind, the ones that stop the whole account first: the order the report lists them in.
KINDS = (
    "integration",
    "reauthorize",
    "operated",
    "no_public_address",
    "unreachable",
    "refused",
    "taken_by_account",
    "taken_elsewhere",
    "not_found",
    # A conversation the rules cover that the account does not subscribe to:
    # past the cap of a rule answering in every channel, a team whose channels
    # cannot be listed, a channel named without its team.
    "not_subscribed",
    "throttled",
    "unavailable",
    "error",
    # The conversation is heard; only the reactions on its answers are read
    # periodically instead of notified.
    "reactions_refused",
)

#: Conversations kept per cause - more than a provider subscribes to for one
#: account - and distinct provider messages, each cut at ``DETAIL_CHARS``.
MAX_TARGETS = 100
MAX_DETAILS = 5
DETAIL_CHARS = 600


@dataclass(frozen=True)
class Problem:
    """One failed step of a sync."""

    kind: str
    #: The provider's or the platform's words: the technical details, the logs, the last error.
    detail: str
    #: What it was about: ``chats``, a mailbox's ``inbox``, ``channel``, or ``None`` for the whole account.
    covers: str | None = None
    name: str | None = None
    #: The permission the refused subscription needs.
    permission: str | None = None
    #: Who holds what was refused or operates the account: an agent account of
    #: the same owner (never another user's) or the host of another address.
    holder: str | None = None
    #: A platform-authored *detail* as a code and its values, shown in place
    #: of it in the technical details.
    note: str | None = None
    note_values: dict[str, str | int] | None = None

    @classmethod
    def of(cls, exc: ProviderError, desired: DesiredSubscription | None = None, **changes: Any) -> Problem:
        """The problem a provider error reports, about *desired* when it was one subscription's."""
        fields: dict[str, Any] = {"kind": exc.kind, "detail": str(exc), **changes}
        if desired is not None:
            fields.setdefault("covers", desired.covers)
            fields.setdefault("name", desired.name)
            if fields["kind"] == "refused":
                fields.setdefault("permission", desired.permission)
        return cls(**fields)


def grouped(problems: list[Problem]) -> list[dict[str, Any]]:
    """*problems* by cause (kind, permission, holder), in :data:`KINDS` order, bounded."""
    groups: dict[tuple[str, str | None, str | None], dict[str, Any]] = {}
    for problem in problems:
        kind = problem.kind if problem.kind in KINDS else "error"
        group = groups.setdefault(
            (kind, problem.permission, problem.holder),
            {
                "kind": kind,
                "permission": problem.permission,
                "holder": problem.holder,
                "count": 0,
                "targets": [],
                "details": [],
                "notes": [],
            },
        )
        group["count"] += 1
        if problem.covers is not None:
            target = {"covers": problem.covers, "name": problem.name}
            if target not in group["targets"] and len(group["targets"]) < MAX_TARGETS:
                group["targets"].append(target)
        if problem.note:
            note = {"code": problem.note, "values": dict(problem.note_values or {})}
            if note not in group["notes"] and len(group["notes"]) < MAX_DETAILS:
                group["notes"].append(note)
            continue
        detail = problem.detail.strip()[:DETAIL_CHARS]
        if detail and detail not in group["details"] and len(group["details"]) < MAX_DETAILS:
            group["details"].append(detail)
    return sorted(groups.values(), key=lambda g: KINDS.index(g["kind"]))


def report(problems: list[Problem], *, wanted: int, heard: int, checked_at: datetime) -> dict[str, Any]:
    """The sync report stored on the account."""
    return {
        "checked_at": checked_at.isoformat(),
        "wanted": wanted,
        "heard": heard,
        "problems": grouped(problems),
    }
