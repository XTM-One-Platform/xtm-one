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

"""Triage: what the agent decides about each message of a mailbox, and what the mailbox then shows.

A chat message is answered or left. An email may be beyond the agent - a
decision, a commitment, information it does not have - or need no answer at
all. On a provider that triages (``ProviderCapabilities.triage``), every
answering turn ends in a structure the platform reads, never a phrase in its
text (invariant 16): the ``decision`` (``reply``, ``needs_owner``,
``no_reply``), the ``reply`` when it answers, and a one-line ``note`` for the
account's owner. The structure is the turn's output schema
(``run_agent_chat(session_output_schema=...)``); :func:`read` reads it back.

What the account then files on the message - a follow-up flag, read, a
category - is the owner's choice (``AgentAccount.mailbox_actions``), turned
into a :class:`~.providers.MessageMark` by :func:`mark_for` and filed by the
provider (``AccountProvider.mark_message``). Pure: no I/O.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.services.agent_accounts.providers import MessageMark
from app.services.agent_accounts.rules import RuleError

DECISIONS = ("reply", "needs_owner", "no_reply")
#: What a decision other than a reply is recorded as (``activity.OUTCOME_GROUPS``).
OUTCOMES = {"needs_owner": "flagged", "no_reply": "silent"}
NOTE_LIMIT = 500
MAX_CATEGORY_CHARS = 255

DEFAULT_MAILBOX_ACTIONS: dict[str, Any] = {
    "flag_needs_owner": True,
    "notify_needs_owner": True,
    "mark_read_replied": True,
    "mark_read_no_reply": False,
    "category_replied": "Answered by AI",
    "category_needs_owner": "Needs your attention",
    "category_no_reply": "No reply needed",
}
_SWITCHES = ("flag_needs_owner", "notify_needs_owner", "mark_read_replied", "mark_read_no_reply")
_CATEGORIES = ("category_replied", "category_needs_owner", "category_no_reply")

_DECISION_HELP = {
    "reply": "reply: you answer the message yourself; the answer is in `reply`.",
    "needs_owner": (
        "needs_owner: it needs the account's owner - a decision, a commitment, or information you do not "
        "have or cannot verify. Nothing is sent; the owner is told why."
    ),
    "no_reply": "no_reply: it needs no answer (information, a thank you, a notification).",
}


@dataclass(frozen=True)
class Triage:
    """What the agent decided about one message."""

    decision: str
    #: The answer, for ``reply``; empty otherwise.
    reply: str = ""
    #: Why, in one line, for the account's owner.
    note: str = ""


def output_schema(*, must_reply: bool) -> dict[str, Any]:
    """The structure an answering turn ends in; a rule that always replies leaves out ``no_reply``.

    Handing a message to the owner stays possible under such a rule: it is the
    honest alternative to an answer the agent cannot stand behind.
    """
    decisions = [d for d in DECISIONS if not (must_reply and d == "no_reply")]
    return {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": decisions,
                "description": " ".join(_DECISION_HELP[d] for d in decisions),
            },
            "reply": {
                "type": "string",
                "description": (
                    "For reply: the body of the answer in Markdown, without the quoted thread or a signature "
                    "(both are added). Otherwise an empty string."
                ),
            },
            "note": {"type": "string", "description": "One sentence for the account's owner: why this decision."},
        },
        "required": ["decision", "reply", "note"],
        "additionalProperties": False,
    }


def _unfenced(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


def unanswered(*, must_reply: bool, note: str = "") -> Triage:
    """A turn that gives no answer: no reply needed, or - when a rule requires one - the owner's to answer."""
    return Triage("needs_owner" if must_reply else "no_reply", "", note)


def read(text: str, *, must_reply: bool) -> Triage | None:
    """The decision a turn answered with; ``None`` when it is not one.

    A ``no_reply`` under a rule that always replies hands the message to the
    owner instead: an answer was required, and the agent gave none.
    """
    try:
        data = json.loads(_unfenced(text or ""))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    decision = data.get("decision")
    if decision not in DECISIONS:
        return None
    reply = data.get("reply") if isinstance(data.get("reply"), str) else ""
    note = data.get("note") if isinstance(data.get("note"), str) else ""
    note = " ".join(note.split())[:NOTE_LIMIT]
    if decision == "reply":
        reply = reply.strip()
        return Triage("reply", reply, note) if reply else None
    if decision == "no_reply":
        return unanswered(must_reply=must_reply, note=note)
    return Triage(decision, "", note)


def normalize_mailbox_actions(raw: Any) -> dict[str, Any] | None:
    """The mailbox actions as stored, every key present; ``None`` for the defaults. Raises :class:`RuleError`."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RuleError("The mailbox actions must be an object.")
    unknown = sorted(set(raw) - set(DEFAULT_MAILBOX_ACTIONS))
    if unknown:
        raise RuleError(f"Unknown mailbox action {unknown[0]!r}.")
    out = dict(DEFAULT_MAILBOX_ACTIONS)
    for key in _SWITCHES:
        if key in raw:
            if not isinstance(raw[key], bool):
                raise RuleError(f"The mailbox action {key!r} must be true or false.")
            out[key] = raw[key]
    for key in _CATEGORIES:
        if key in raw:
            value = raw[key]
            if value is None:
                value = ""
            if not isinstance(value, str):
                raise RuleError(f"The category {key!r} must be text.")
            value = value.strip()
            if len(value) > MAX_CATEGORY_CHARS:
                raise RuleError(f"A category is at most {MAX_CATEGORY_CHARS} characters.")
            out[key] = value
    return out


def merge_mailbox_actions(stored: Any, patch: Any) -> dict[str, Any] | None:
    """*patch* merged over the actions in effect, as stored. Raises :class:`RuleError`.

    A key left out keeps its stored value: a caller switching one action never
    resets the others to the defaults. ``None`` keeps the defaults.
    """
    if patch is None:
        return None
    if not isinstance(patch, dict):
        return normalize_mailbox_actions(patch)
    unknown = sorted(set(patch) - set(DEFAULT_MAILBOX_ACTIONS))
    if unknown:
        raise RuleError(f"Unknown mailbox action {unknown[0]!r}.")
    return normalize_mailbox_actions({**effective_mailbox_actions(stored), **patch})


def effective_mailbox_actions(stored: Any) -> dict[str, Any]:
    """The actions in effect: the stored ones over the defaults, a value of the wrong type ignored."""
    out = dict(DEFAULT_MAILBOX_ACTIONS)
    if isinstance(stored, dict):
        for key in _SWITCHES:
            if isinstance(stored.get(key), bool):
                out[key] = stored[key]
        for key in _CATEGORIES:
            if isinstance(stored.get(key), str):
                out[key] = stored[key].strip()[:MAX_CATEGORY_CHARS]
    return out


def mark_for(decision: str, actions: dict[str, Any], *, shadow: bool = False) -> MessageMark:
    """What the account files on a message the agent decided *decision* about.

    In shadow mode only the follow-up flag of a handed-over message: nothing
    is marked read or filed, and nothing is flagged for any other decision.
    """
    if shadow:
        return MessageMark(flag=decision == "needs_owner" and bool(actions["flag_needs_owner"]))
    if decision == "reply":
        return MessageMark(read=bool(actions["mark_read_replied"]), category=actions["category_replied"] or None)
    if decision == "needs_owner":
        return MessageMark(flag=bool(actions["flag_needs_owner"]), category=actions["category_needs_owner"] or None)
    return MessageMark(read=bool(actions["mark_read_no_reply"]), category=actions["category_no_reply"] or None)
