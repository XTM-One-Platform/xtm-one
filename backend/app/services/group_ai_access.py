# SPDX-License-Identifier: Apache-2.0

"""Is AI switched off for this person's groups?

An administrator can switch AI off for the members of a group
(``Group.ai_disabled``). A person in one such group has no AI - whatever their
other groups say, and an administrator is held to it like everyone else: the
AI gates are not what the settings need, so nobody is locked out of the
platform by it. This module is the ONE reader of that flag; a second reader is
a defect findable by grep, as for ``ai_state`` (the platform-wide switch it sits
beside: the platform switch off means off for everyone, this one means off for
the people of a group).

What it decides, through the readers that already ask the platform switch:

* the request gates (``deps.require_ai_enabled`` / ``require_agentic_ai`` /
  ``require_chat_enabled``) refuse the member with the platform switch's own
  ``ai_disabled`` 403;
* background work done on a person's behalf (an assignment run acting as them,
  a flow they own, a task they delegated) is held like the platform switch
  holds it (``agentic_gate.group_verdict``), and a loop over several people's
  work skips theirs instead of stopping (``agentic_gate.refusal_is_personal``);
* the LLM router refuses them every provider (``llm.provider_access``), so a
  path no gate names - memory, compaction, embeddings of their content, the MCP
  and A2A endpoints, an external-channel turn - still runs no model for them.

The stored column is negative (``ai_disabled``) like the platform column; the
wire is positive - ``ai_enabled`` on the groups API, flipped only by
:func:`group_ai_enabled` / :func:`set_group_ai_enabled` - and ``GET /auth/me``
reports ``ai_disabled_by_group`` with the group that disables it.
"""

from __future__ import annotations

import logging
import time
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group import Group, GroupMember

logger = logging.getLogger(__name__)

#: How long the set of AI-disabled groups is reused before it is read again.
#:
#: Same window and same reasoning as ``ai_state.CACHE_TTL_SECONDS``: the steady
#: state of an install that uses no AI-disabled group is one empty set held in
#: memory, so the router (which asks on every model resolution) does no database
#: work for the feature. Each replica converges within one TTL of a change; the
#: replica that served it converges at once (:func:`invalidate_cache`).
CACHE_TTL_SECONDS = 5.0

#: What a member is told. No group name: a 403 body is read by whatever client
#: made the call, and the member's own profile says which group it is.
AI_DISABLED_BY_GROUP_MESSAGE = (
    "AI is disabled for one of your groups. Ask an administrator if you need it."
)

# (ids of the groups that disable AI, monotonic expiry). Process-local.
_cache: tuple[frozenset[str], float] | None = None


def invalidate_cache() -> None:
    """Drop the cached set. Call it AFTER the commit of a change to the flag or
    to a group's membership; before it, a concurrent read would re-cache the old
    set for a whole TTL."""
    global _cache
    _cache = None


async def invalidate_cache_after_commit() -> None:
    """:func:`invalidate_cache` in the shape ``deps.AfterCommit`` awaits."""
    invalidate_cache()


def _as_uuid(user_id: object) -> uuid.UUID | None:
    if user_id is None:
        return None
    try:
        return user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except (ValueError, TypeError, AttributeError):
        return None


async def disabled_group_ids(db: AsyncSession) -> frozenset[str]:
    """The ids of every group that switches AI off, cached for the TTL."""
    global _cache
    cached = _cache
    if cached is not None and cached[1] > time.monotonic():
        return cached[0]
    rows = await db.execute(select(Group.id).where(Group.ai_disabled.is_(True)))
    ids = frozenset(str(group_id) for group_id in rows.scalars().all())
    _cache = (ids, time.monotonic() + CACHE_TTL_SECONDS)
    return ids


async def member_of_disabled_group(
    db: AsyncSession, user_id: object, *, group_ids: frozenset[str] | None = None
) -> bool:
    """Whether *user_id* belongs to a group that switches AI off.

    A caller with no account (``None``, ``"anonymous"``, ``"system"``) belongs
    to no group: the platform switch is what decides for them.

    *group_ids* is the person's groups when the caller already loaded them (the
    router's audience does), which spares the membership query.
    """
    disabled = await disabled_group_ids(db)
    if not disabled:
        return False
    if group_ids is not None:
        return bool(disabled & group_ids)
    person = _as_uuid(user_id)
    if person is None:
        return False
    row = await db.execute(
        select(GroupMember.id)
        .where(
            GroupMember.user_id == person,
            GroupMember.group_id.in_([uuid.UUID(g) for g in disabled]),
        )
        .limit(1)
    )
    return row.first() is not None


async def member_of_disabled_group_isolated(db: AsyncSession, user_id: object) -> bool:
    """:func:`member_of_disabled_group` for a caller mid-transaction.

    The background gate asks it on the session of the work it decides on (a
    run, a delegated task), so the read must not touch that session's state:
    ``no_autoflush`` keeps its pending changes unflushed, and a Core savepoint
    absorbs a failed SELECT, which PostgreSQL would otherwise turn into an
    aborted transaction for the caller's next statement - the recipe and the
    reasons of ``ai_state._read_override_isolated``. A session-like stand-in
    (a test double) is read plainly.
    """
    cached = _cache
    if cached is not None and cached[1] > time.monotonic() and not cached[0]:
        # No group switches AI off: no connection is even taken.
        return False
    if not isinstance(db, AsyncSession):
        return await member_of_disabled_group(db, user_id)
    conn = await db.connection()
    with db.no_autoflush:
        async with conn.begin_nested():
            return await member_of_disabled_group(db, user_id)


def group_ai_enabled(group: object) -> bool:
    """Whether *group* leaves AI on for its members: the wire's positive form.

    With :func:`set_group_ai_enabled`, the only places the polarity flips, as
    ``ai_state`` does for the platform switch: the API speaks ``ai_enabled``
    and never names nor writes the stored ``ai_disabled``
    (``tests/test_ai_toggle.py``).
    """
    return not getattr(group, "ai_disabled", False)


def set_group_ai_enabled(group: Group, enabled: bool) -> bool:
    """Switch AI on or off for *group*'s members; returns the previous wire value.

    The caller commits, then calls :func:`invalidate_cache`.
    """
    previous = group_ai_enabled(group)
    group.ai_disabled = not enabled
    return previous


async def disabling_group_names(db: AsyncSession, user_id: object) -> list[str]:
    """The names of the groups that switch AI off for *user_id*, sorted.

    For telling the person which group it is (``GET /auth/me``); gates ask
    :func:`member_of_disabled_group`, which stops at the first hit.
    """
    disabled = await disabled_group_ids(db)
    person = _as_uuid(user_id)
    if not disabled or person is None:
        return []
    rows = await db.execute(
        select(Group.name)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .where(
            GroupMember.user_id == person,
            Group.id.in_([uuid.UUID(g) for g in disabled]),
        )
    )
    return sorted(rows.scalars().all(), key=str.lower)
