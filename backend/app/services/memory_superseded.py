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

"""Keep what no longer holds out of the Redis memory tiers (#11861).

The persistent tier leaves a superseded or retired entry out of every prompt
(``persistent_memory._active_for_reader``). The Redis tiers are text: the
medium-term summary was consolidated from run summaries that quoted the
entry before it was replaced, and the short-term list still holds those run
summaries. Both reached the system prompt verbatim, so a rule an agent had
replaced kept steering it for up to 30 days.

``load_superseded`` reads the entries that stopped holding in a lane (with
their successor's text); ``drop_superseded`` removes from the summary each
line, and from the short-term list each entry, that states one of them more
closely than its successor. The prompt loaders, the consolidation (so the
rebuilt summary loses the line for good) and the Memory Steward's promotion
read memory through it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import aliased

from app.core.utils import format_exception_message, safe_uuid
from app.models.agent_memory_entry import AgentMemoryEntry

logger = logging.getLogger(__name__)

#: How far back a supersede still matters: the medium-term summary lives 30
#: days (``agent_memory.MEDIUM_TERM_TTL``), a run summary 24 hours.
LOOKBACK = timedelta(days=30)
#: Superseded entries one read weighs, newest first.
MAX_SUPERSEDED = 200
#: The read never holds a prompt load back longer than this; past it the
#: memory is injected unfiltered, as before.
READ_TIMEOUT_SECS = 2.0
#: Share of a superseded entry's words a line must carry to state it.
MATCH_SHARE = 0.6
#: Words shorter than this carry no meaning of their own here.
_MIN_WORD = 3
_WORD_RE = re.compile(r"[\w'-]+", re.UNICODE)


@dataclass(frozen=True)
class Superseded:
    """An entry that no longer holds, and the text that replaced it (``None``: retired)."""

    content: str
    successor: str | None = None


def _words(text: str) -> frozenset[str]:
    """The meaningful words of *text*: three letters or more, or any number."""
    return frozenset(
        word
        for word in _WORD_RE.findall((text or "").lower())
        if len(word) >= _MIN_WORD or any(ch.isdigit() for ch in word)
    )


def _phrase(text: str) -> str:
    return " ".join((text or "").lower().split()).strip(" .;,:!?")


def _share(line_words: frozenset[str], entry_words: frozenset[str]) -> float:
    if not entry_words:
        return 0.0
    return len(line_words & entry_words) / len(entry_words)


def states_superseded(line: str, superseded: list[Superseded]) -> bool:
    """Whether *line* states an entry of *superseded* more closely than its successor."""
    normalized = _phrase(line)
    if not normalized:
        return False
    line_words = _words(line)
    for entry in superseded:
        old = _phrase(entry.content)
        if old and old in normalized:
            new = _phrase(entry.successor or "")
            if not new or new not in normalized:
                return True
            continue
        old_share = _share(line_words, _words(entry.content))
        if old_share < MATCH_SHARE:
            continue
        if entry.successor is None or old_share > _share(line_words, _words(entry.successor)):
            return True
    return False


def drop_superseded_lines(text: str, superseded: list[Superseded]) -> str:
    """*text* without the lines that state a superseded entry."""
    if not text or not superseded:
        return text
    kept = [line for line in text.splitlines() if not states_superseded(line, superseded)]
    return "\n".join(kept).strip()


def drop_superseded(memory: dict, superseded: list[Superseded]) -> dict:
    """*memory* as ``agent_memory.load_memory`` returns it, without what no longer holds."""
    if not superseded:
        return memory
    short = [
        entry
        for entry in memory.get("short_term") or []
        if not states_superseded(str(entry.get("summary") or ""), superseded)
    ]
    return {
        **memory,
        "short_term": short,
        "medium_term": drop_superseded_lines(memory.get("medium_term") or "", superseded),
    }


def _sessions():
    """The session factory the read opens its own session from (a test points it at its database)."""
    from app.core.database import async_session

    return async_session


async def _read(agent_id: uuid.UUID, user_id: uuid.UUID | None) -> list[Superseded]:
    successor = aliased(AgentMemoryEntry)
    pools = [AgentMemoryEntry.user_id.is_(None)]
    if user_id is not None:
        pools.append(AgentMemoryEntry.user_id == user_id)
    async with _sessions()() as session:
        rows = (
            await session.execute(
                select(AgentMemoryEntry.content, successor.content)
                .outerjoin(successor, successor.id == AgentMemoryEntry.superseded_by_id)
                .where(
                    AgentMemoryEntry.agent_id == agent_id,
                    or_(*pools),
                    AgentMemoryEntry.superseded_at.is_not(None),
                    AgentMemoryEntry.superseded_at >= datetime.now(UTC) - LOOKBACK,
                )
                .order_by(AgentMemoryEntry.superseded_at.desc())
                .limit(MAX_SUPERSEDED)
            )
        ).all()
    return [Superseded(content, new) for content, new in rows if content]


async def load_superseded(agent_id: str, scope_id: str | None = None) -> list[Superseded]:
    """The entries that stopped holding in the lane of *agent_id* (and of the user *scope_id*).

    Never raises: a read that fails or runs late answers nothing, and the
    memory goes in unfiltered.
    """
    aid = safe_uuid(agent_id)
    if aid is None:
        return []
    try:
        return await asyncio.wait_for(_read(aid, safe_uuid(scope_id)), READ_TIMEOUT_SECS)
    except Exception as exc:  # the filter must never cost a run its memory
        logger.warning(
            "Superseded memory entries of agent %s not read, memory injected unfiltered: %s",
            agent_id,
            format_exception_message(exc),
        )
        return []
