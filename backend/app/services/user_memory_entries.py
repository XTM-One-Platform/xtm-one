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

"""What the cross-agent user memory holds, entry by entry, and who reads it.

Each entry carries:

* ``scope``: ``global`` - the user's PROFILE, read by every agent - or
  ``agent:<id>`` - a NOTE the agent's work left about the user, read by that
  agent only;
* ``owner_agent_id``: the agent a note belongs to, ``None`` for the profile;
* ``created_at`` / ``last_verified_at`` / ``ttl_seconds`` / ``perishable``: a
  note about something that can change lives ``PERISHABLE_TTL_SECONDS`` after
  it was last stated, and is shown as unverified.

Which statement is profile and which is a note, and whether a note is
perishable, is the consolidation model's structured answer
(``user_memory._CONSOLIDATION_PROMPT``); the owner of a note is the agent of
the interaction it came from, never read from its text. Nothing here knows
what a statement is about.

:func:`visible_to` is the one read rule. Pure functions: no Redis, no model.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

GLOBAL_SCOPE = "global"
AGENT_SCOPE_PREFIX = "agent:"

#: How long a note about changeable state is believed once last stated.
PERISHABLE_TTL_SECONDS = 72 * 3600

UNVERIFIED_LABEL = "[UNVERIFIED as of {date}, verify against the live system]"

PROFILE_HEADER = "── User Profile (about the user, shared across all agents) ──"
OWN_NOTES_HEADER = "── Your notes from earlier work for this user (this agent only) ──"


def agent_scope(agent_id: object) -> str:
    return f"{AGENT_SCOPE_PREFIX}{agent_id}"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class MemoryEntry:
    text: str
    scope: str = GLOBAL_SCOPE
    owner_agent_id: str | None = None
    created_at: str = ""
    last_verified_at: str = ""
    ttl_seconds: int | None = None
    perishable: bool = False
    id: str = field(default_factory=lambda: secrets.token_hex(6))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: object) -> MemoryEntry | None:
        """The entry *raw* stores, ``None`` for anything that is not one."""
        if not isinstance(raw, dict):
            return None
        text = raw.get("text")
        scope = raw.get("scope")
        if not isinstance(text, str) or not text.strip() or not isinstance(scope, str):
            return None
        owner = str(raw["owner_agent_id"]) if raw.get("owner_agent_id") else None
        if scope != GLOBAL_SCOPE and not (owner and scope == agent_scope(owner)):
            return None
        ttl = raw.get("ttl_seconds")
        return cls(
            text=text.strip(),
            scope=scope,
            owner_agent_id=owner if scope != GLOBAL_SCOPE else None,
            created_at=str(raw.get("created_at") or ""),
            last_verified_at=str(raw.get("last_verified_at") or raw.get("created_at") or ""),
            ttl_seconds=ttl if isinstance(ttl, int) and not isinstance(ttl, bool) and ttl > 0 else None,
            perishable=raw.get("perishable") is True,
            id=str(raw.get("id") or secrets.token_hex(6)),
        )

    @property
    def is_global(self) -> bool:
        return self.scope == GLOBAL_SCOPE

    def expires_at(self) -> datetime | None:
        if not self.ttl_seconds:
            return None
        verified = _parse_iso(self.last_verified_at) or _parse_iso(self.created_at)
        return verified + timedelta(seconds=self.ttl_seconds) if verified else None

    def is_live(self, now: datetime) -> bool:
        expiry = self.expires_at()
        return expiry is None or expiry > now


def entries_from_raw(raw: object) -> list[MemoryEntry]:
    """The entries a stored JSON list holds; anything else is skipped."""
    if not isinstance(raw, list):
        return []
    return [entry for entry in map(MemoryEntry.from_dict, raw) if entry is not None]


def new_entry(
    text: str,
    *,
    now: datetime,
    owner_agent_id: str | None = None,
    perishable: bool = False,
    verified_at: datetime | None = None,
) -> MemoryEntry:
    """A profile statement (no owner) or a note of *owner_agent_id*."""
    stamp = _iso(verified_at or now)
    owner = str(owner_agent_id) if owner_agent_id else None
    perishable = perishable and owner is not None
    return MemoryEntry(
        text=" ".join(text.split()),
        scope=agent_scope(owner) if owner else GLOBAL_SCOPE,
        owner_agent_id=owner,
        created_at=stamp,
        last_verified_at=stamp,
        ttl_seconds=PERISHABLE_TTL_SECONDS if perishable else None,
        perishable=perishable,
    )


def _normalized(text: str) -> str:
    return " ".join(text.lower().split())


def restated(entry: MemoryEntry, *, now: datetime) -> MemoryEntry:
    """*entry* stated again at *now*: its expiry counts from there."""
    entry.last_verified_at = _iso(now)
    return entry


def keep_history(entry: MemoryEntry, previous: Iterable[MemoryEntry]) -> MemoryEntry:
    """*entry* with the id, timestamps and expiry of the same statement already stored.

    A statement copied forward unchanged is not a new confirmation of it: it
    keeps the date it was last stated, and once marked as something that can
    change it keeps its expiry, so a stale note still expires.
    """
    key = (_normalized(entry.text), entry.scope)
    for old in previous:
        if (_normalized(old.text), old.scope) == key:
            entry.id = old.id
            entry.created_at = old.created_at or entry.created_at
            entry.last_verified_at = old.last_verified_at or entry.last_verified_at
            if old.perishable:
                entry.perishable = True
                entry.ttl_seconds = old.ttl_seconds
            break
    return entry


# ── Who reads what ───────────────────────────────────────────────────────────


def _owned_by(entry: MemoryEntry, agent: str | None) -> bool:
    return bool(agent) and entry.scope == agent_scope(agent)


def visible_to(entries: Iterable[MemoryEntry], *, agent_id: object, now: datetime) -> list[MemoryEntry]:
    """What *agent_id*'s context may carry: the one read rule.

    The user's profile and the agent's own live notes, never another agent's
    note. No agent (a caller that cannot name one): the profile only.
    """
    agent = str(agent_id) if agent_id else None
    return [entry for entry in entries if entry.is_live(now) and (entry.is_global or _owned_by(entry, agent))]


def split_visible(
    entries: Sequence[MemoryEntry], *, agent_id: object
) -> tuple[list[MemoryEntry], list[MemoryEntry]]:
    """*entries* (already :func:`visible_to`) as (profile, the agent's own notes)."""
    agent = str(agent_id) if agent_id else None
    return (
        [entry for entry in entries if entry.is_global],
        [entry for entry in entries if _owned_by(entry, agent)],
    )


def render_line(entry: MemoryEntry) -> str:
    text = entry.text
    if entry.perishable:
        date = (entry.last_verified_at or entry.created_at)[:10] or "an unknown date"
        text = f"{UNVERIFIED_LABEL.format(date=date)} {text}"
    return f"- {text}"


def render_profile(entries: Iterable[MemoryEntry]) -> str:
    """The user's profile as bullet lines, the form a profile rewrite reads and writes."""
    return "\n".join(render_line(entry) for entry in entries if entry.is_global)


_BULLET_RE = re.compile(r"^\s*(?:[-*•·]|\d+[.)])\s+")


def bullet_lines(text: str) -> list[str]:
    """The statements of a bullet list, one per non-empty line, bullets dropped."""
    lines = (_BULLET_RE.sub("", line).strip() for line in (text or "").splitlines())
    return [line for line in lines if line]
