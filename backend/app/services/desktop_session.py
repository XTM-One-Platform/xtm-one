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

"""XTM One Desktop - a chat turn sent from the machine the user sits at.

The Desktop app is the runner (the native OS-control daemon) plus a
real chat window: it loads this platform's web app in a native webview and
injects the identity of the runner it embeds. Every chat turn sent from that
window carries ``desktop_runner_id``, and this module is how the chat layer
turns that id into a turn where the assistant can act on the machine:

* **context** - which enrolled machine the user is sitting at, resolved per
  turn against the CALLING user's runner visibility
  (:func:`load_desktop_context`). A runner the user cannot see - another
  user's private machine, a revoked or deleted one - drops the id silently:
  the turn runs as an ordinary chat, and the agent is told nothing about a
  machine the user may not target.
* **capability** - the ``computer_use`` suite is guaranteed on every desktop
  turn, whatever the agent's own configuration or the session picker says
  (:func:`ensure_desktop_tools`), and the local runner is pinned as the
  DEFAULT target of the suite: ``run_shell`` / ``read_file`` / ``write_file``
  / ``list_dir`` / ``computer_screenshot`` / ``browser_control`` without a
  ``runner_id`` act on this machine. Fleet targeting by ``runner_id`` keeps
  working. The pin is enforced in the tool builder
  (``builtin_tools/computer_use.py``), and the agent learns which machine
  "this machine" is from the TOOL DESCRIPTIONS - deliberately not from a
  system-prompt block (AGENTS.md invariant 15).
* **interactivity** - a desktop turn is a human watching a chat, so a shell
  command the runner's policy holds for approval is offered INLINE (a
  ``tool_progress`` frame the chat renders as an approval card) and waited
  for, instead of being queued and reported as "approve it later".

The runner's own permission model is untouched and still gates every command:
the desktop pin decides WHICH machine a call defaults to, never WHAT it may
do there. The headless flavor (servers, VMs, MDM) never sends a desktop id and
is unaffected.

Mirrors the shape of ``presentation_studio.py`` on purpose: one small
dataclass, one loader that fails open to ``None``, one idempotent tool union.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message, safe_uuid
from app.models.runner import Runner

logger = logging.getLogger(__name__)

#: Built-in tool ids every desktop turn carries, on top of the agent's own.
DESKTOP_TOOL_IDS: tuple[str, ...] = ("computer_use",)

#: Product name of the native client, as every human-facing surface spells it.
DESKTOP_PRODUCT_NAME = "XTM One Desktop"


@dataclass(frozen=True, slots=True)
class DesktopContext:
    """The machine a chat turn was sent from, as the calling user may target it."""

    runner: Runner
    #: Whether the calling user is an administrator - the DECIDING role for a
    #: held command on a company-managed machine (``can_manage``), carried
    #: separately from targeting, which never uses admin-wide visibility.
    approver_is_admin: bool = False

    @property
    def runner_id(self) -> str:
        return str(self.runner.id)


def parse_desktop_runner_id(value: object) -> uuid.UUID | None:
    """The client's ``desktop_runner_id`` as a UUID, or ``None``.

    The client sends what the native shell injected; a malformed or absent
    value is simply "not a desktop turn" - never an error the user has to see.
    """
    if not isinstance(value, str):
        return None
    return safe_uuid(value.strip())


async def load_desktop_context(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    runner_id: object,
    user_group_ids: list[str] | None = None,
) -> DesktopContext | None:
    """Resolve the machine a turn was sent from, or ``None``.

    ``None`` both when no id was sent and when the runner is not (or no
    longer) visible to *user_id* - the two are indistinguishable on purpose.

    Visibility is the NORMAL three-tier predicate (own + group-shared +
    company-managed) with no admin bypass: targeting is an execution power,
    and an admin's agent must never be able to drive another user's private
    machine, whatever the caller's role - the same rule the tool builder
    applies (``computer_use._eligible_runners``). A revoked runner is not a
    target either: revocation is a security tombstone.

    Online-ness is deliberately NOT required here: the desktop daemon connects
    moments after the window opens, and the tools report an offline machine
    precisely when a call is made.

    Fail-open: a lookup error yields ``None`` and a debug log - a chat turn
    must never fail because the desktop hint could not be checked. The lookup
    runs isolated for that reason (:func:`_isolated_read`): a failed statement
    aborts the PostgreSQL transaction it ran in, and swallowing the error would
    otherwise hand the caller a session on which every later query fails too.
    """
    runner_uuid = parse_desktop_runner_id(runner_id)
    if runner_uuid is None:
        return None
    try:
        found = await _isolated_read(
            db, lambda: _read_desktop_runner(db, user_id, runner_uuid, user_group_ids)
        )
    except Exception as exc:
        logger.debug(
            "desktop context skipped for runner %s: %s",
            runner_uuid,
            format_exception_message(exc),
        )
        return None
    if found is None:
        return None
    runner, approver_is_admin = found
    return DesktopContext(runner=runner, approver_is_admin=approver_is_admin)


async def _read_desktop_runner(
    db: AsyncSession,
    user_id: uuid.UUID,
    runner_uuid: uuid.UUID,
    user_group_ids: list[str] | None,
) -> tuple[Runner, bool] | None:
    """The visible, active runner and whether the caller is an administrator."""
    from sqlalchemy import select

    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.models.user import User

    group_ids = (
        user_group_ids if user_group_ids is not None else await get_user_group_ids(db, user_id)
    )
    result = await db.execute(
        select(Runner).where(
            Runner.id == runner_uuid,
            Runner.is_active.is_(True),
            user_visible_filter(Runner, user_id, group_ids),
        )
    )
    runner = result.scalar_one_or_none()
    if runner is None:
        return None
    # The caller's real role, for the approval decision only: an administrator
    # is who may decide a held command on a company-managed machine. Targeting
    # stays non-admin.
    role = await db.execute(select(User.role).where(User.id == user_id))
    return runner, role.scalar_one_or_none() == "admin"


async def _isolated_read[T](db: AsyncSession, read: Callable[[], Awaitable[T]]) -> T:
    """Run *read* without touching the caller's ORM state or transaction.

    Same recipe as ``ai_state._read_override_isolated`` (the reasons are
    measured there): a **Core** savepoint on the session's connection, not
    ``Session.begin_nested`` - which flushes the caller's pending entities
    before opening the savepoint and, if the read then fails, expires their
    dirty instances - and ``no_autoflush`` so ``session.execute`` does not
    flush that pending state before the SELECT either. A failed SELECT aborts
    the PostgreSQL transaction; the Core savepoint absorbs that, so the
    caller's next statement still works. A minimal session-like object (the
    fakes several callers' tests pass) is read plainly.
    """
    connection = getattr(db, "connection", None)
    no_autoflush = getattr(db, "no_autoflush", None)
    if connection is None or no_autoflush is None:
        return await read()
    conn = await connection()
    with no_autoflush:
        async with conn.begin_nested():
            return await read()


def ensure_desktop_tools(builtin_tool_ids: list[str] | None) -> list[str]:
    """The turn's built-in tool ids with the desktop's guaranteed ones appended.

    Order is preserved and nothing is removed: the agent keeps every tool it
    had, and gains the ``computer_use`` suite it may have lacked. Idempotent.
    """
    resolved = list(builtin_tool_ids or [])
    for tool_id in DESKTOP_TOOL_IDS:
        if tool_id not in resolved:
            resolved.append(tool_id)
    return resolved
