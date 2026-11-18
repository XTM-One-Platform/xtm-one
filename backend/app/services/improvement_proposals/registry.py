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

"""What each proposal type does to its target: one handler per type.

The generic lifecycle (``lifecycle.py``) never touches a target itself; it
asks the type's handler to load it, say who manages it, check that the change
still applies, write it as a new version and undo it. Each type's module
registers its handler with ``register_proposal_type`` and is named in
``_HANDLER_MODULES``. A type without a handler is
refused at filing, so no proposal waits on code that does not exist.
"""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from app.models.improvement_proposal import STATUS_WATCHING, TARGET_OF

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.improvement_proposal import ImprovementProposal
    from app.models.user import User
    from app.services.improvement_proposals.lifecycle import ProposalSpec


@dataclass(frozen=True, slots=True)
class TargetInfo:
    """Who a target belongs to, as the approval rule reads it."""

    name: str
    #: The agent the target belongs to, recorded on the proposal (cascade key).
    agent_id: uuid.UUID | None
    is_company_managed: bool
    #: The row whose owner and shares decide who manages the target (the agent
    #: for a persona or an assignment, the skill, the flow).
    managed: Any


@dataclass(frozen=True, slots=True)
class Applied:
    """What applying wrote."""

    version_number: int | None
    #: ``watching`` for a change written in the approval's transaction;
    #: ``applying`` / ``shadow`` for one that goes on in steps (level 7).
    status: str = STATUS_WATCHING
    #: The re-evaluation the watch reads, when the type measures one.
    candidate_run_id: uuid.UUID | None = None
    #: Why the evaluation the proposal names does not judge the change (no
    #: like-for-like candidate run), kept in its outcome note.
    unmeasured: str | None = None


@dataclass(frozen=True, slots=True)
class LiveVerdict:
    """A live watcher's reading of an applied change; ``regressed=None``: no reading yet."""

    regressed: bool | None
    note: str | None = None


LiveWatcher = Callable[["AsyncSession", "ImprovementProposal"], Awaitable[LiveVerdict]]


class ProposalHandler(Protocol):
    proposal_type: str
    target_type: str
    #: How the people deciding call one ("persona rewrite").
    noun: str
    #: What changes, for the notes ("persona").
    subject: str
    #: A handler that settles its own proposals through ``set_proposal_status``
    #: (agent-to-flow's shadow and switch-over); the generic settle skips them.
    owns_watch: bool

    async def load_target(self, db: AsyncSession, target_id: uuid.UUID, *, lock: bool) -> Any | None:
        """The target; *lock*: ``FOR UPDATE``, re-read over what the session holds (``locked``)."""
        ...

    def describe(self, target: Any) -> TargetInfo: ...

    def manage_error(
        self, target: Any, user: User, user_group_ids: list[str] | None, *, action: str
    ) -> str | None:
        """The gate of a manual edit of the target (built-in, company-managed, config grant)."""
        ...

    async def validate(self, db: AsyncSession, spec: ProposalSpec, target: Any) -> None:
        """Raise ``ProposalError`` when the change breaks the type's rules."""
        ...

    async def stale_error(self, db: AsyncSession, proposal: ImprovementProposal, target: Any) -> str | None:
        """Why the change no longer applies to the target as it is now, or ``None``."""
        ...

    async def apply(
        self, db: AsyncSession, proposal: ImprovementProposal, target: Any, approver: User
    ) -> Applied:
        """Write the change as a new version, audited; raise ``ProposalError`` before any write."""
        ...

    async def is_still_applied(self, db: AsyncSession, proposal: ImprovementProposal, target: Any) -> bool: ...

    async def rollback(
        self, db: AsyncSession, proposal: ImprovementProposal, target: Any, reason: str
    ) -> int | None:
        """Put the target back as a new version, system-audited; its version number."""
        ...


_HANDLERS: dict[str, ProposalHandler] = {}
_WATCHERS: dict[str, LiveWatcher] = {}
#: Modules that register a handler on import, loaded on first use so every
#: process (API, worker, scheduler) knows every type.
_HANDLER_MODULES: tuple[str, ...] = (
    "app.services.improvement_proposals.persona",
    "app.services.improvement_proposals.skill",
    "app.services.improvement_proposals.assignment_prompt",
    # The four flow types (#8260): flow_repair, flow_optimization, new_flow, agent_to_flow.
    "app.services.health.flow_proposals",
)
_loaded = False


def locked(query: Any, lock: bool) -> Any:
    """*query* ``FOR UPDATE`` when *lock*, refreshing a row the session already
    holds: what was read before the lock may have been changed by the
    transaction it waited on."""
    return query.with_for_update().execution_options(populate_existing=True) if lock else query


def register_proposal_type(proposal_type: str, handler: ProposalHandler) -> None:
    if handler.proposal_type != proposal_type or TARGET_OF.get(proposal_type) != handler.target_type:
        raise ValueError(f"Handler does not match the proposal type {proposal_type!r}")
    _HANDLERS[proposal_type] = handler


def register_live_watcher(proposal_type: str, watcher: LiveWatcher) -> None:
    """Live metrics read during the watch window (level 7 / 8): a regression rolls back."""
    _WATCHERS[proposal_type] = watcher


def _load() -> None:
    global _loaded
    if not _loaded:
        for module in _HANDLER_MODULES:
            importlib.import_module(module)
        _loaded = True


def handler_for(proposal_type: str) -> ProposalHandler | None:
    _load()
    return _HANDLERS.get(proposal_type)


def handler_for_target(target_type: str) -> ProposalHandler | None:
    """A handler of *target_type*: what loads and describes such a target,
    whichever type of proposal is read on it."""
    _load()
    return next((handler for handler in _HANDLERS.values() if handler.target_type == target_type), None)


def live_watcher_for(proposal_type: str) -> LiveWatcher | None:
    _load()
    return _WATCHERS.get(proposal_type)


def live_watched_types() -> tuple[str, ...]:
    """The types a live watcher reads during their watch window."""
    _load()
    return tuple(_WATCHERS)


def self_watched_types() -> tuple[str, ...]:
    _load()
    return tuple(name for name, handler in _HANDLERS.items() if handler.owns_watch)
