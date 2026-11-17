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

"""Whether a run's acting user can see the integration its events come from.

An ``integration_event`` assignment names the integration it listens to by id
(``trigger_config.integration_id``) or falls back to its agent's first binding
of the provider; a flow's ``trigger.integration_event`` node names one too. The
pollers read that integration's events through its stored credentials, behind
the personal OAuth token gate
(``integration_credentials.oauth_credentials_usable_clause``), and hand them to
a run.

The acting user is the identity the run acts as: for an assignment, its "run
as" user, then its owner, then the agent's owner (``run.user_id`` and
``effective_user_id`` in ``assignment_engine``); for a flow, its owner. Seeing
the integration is the three-tier rule of ``user_visible_filter`` (own, user
grant, group share, company-managed unless ``access_restricted``), an admin
seeing every integration. An owner-less company-managed assignment has no
acting user and keeps the OAuth gate alone. Owner-less flows never poll.

The events are delivered whether or not the acting user can see the
integration: nothing is withheld at run time (maintainer decision of
2026-09-27). A run acting as a user who cannot see it is never silent
either: one WARNING per assignment (or flow node) and hour names it
(:func:`log_unseen_trigger_integration`), and the security audit lists the
assignments and flows it applies to (:func:`assignment_trigger_visibility`,
:func:`flow_trigger_visibility`).

A knowledge base sync source bound to an integration
(``KBSyncSource.integration_id``) is the same case: every sync of it,
scheduled or started with "Sync now", reads through that integration as
bound, and it syncs as its configuring user, else the knowledge base's owner
(:func:`sync_source_acting_user_id`, the scheduler's rule). Who may bind it is
checked where the binding is written. A source whose user cannot see its
integration is logged once an hour (:func:`log_unseen_sync_source_integration`)
and listed by the audit (:func:`sync_source_integration_visibility`).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import safe_uuid
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.integration import Integration
from app.models.user import User
from app.services.integration_credentials import normalize_acting_user_id

logger = logging.getLogger(__name__)

#: How often one assignment (or flow node) whose acting user cannot see its
#: trigger integration is logged again.
UNSEEN_LOG_INTERVAL_SECONDS = 3600.0
_MAX_TRACKED_TARGETS = 10_000
_last_unseen_log: dict[tuple[str, str], float] = {}


def trigger_integration_id(trigger_config: Any, agent: Any) -> Any:
    """The integration an assignment's integration-event trigger reads.

    The one its config names, else the agent's first binding of the trigger's
    provider - the event poller's rule, returned as stored.
    """
    config = trigger_config if isinstance(trigger_config, dict) else {}
    provider = config.get("provider", "")
    bound = (getattr(agent, "integration_bindings", None) or {}).get(provider) or []
    return config.get("integration_id") or (bound[0] if bound else None)


def assignment_acting_user_id(assignment: Any, agent: Any) -> uuid.UUID | None:
    """The identity an automatic run of *assignment* acts as, ``None`` for none.

    ``trigger_assignment`` gives an automatic run the assignment's "run as"
    user, then its owner; the run then acts as ``run.user_id``, else the
    agent's owner.
    """
    for candidate in (
        getattr(assignment, "run_as_user_id", None),
        getattr(assignment, "user_id", None),
        getattr(agent, "user_id", None),
    ):
        acting = normalize_acting_user_id(candidate)
        if acting is not None:
            return acting
    return None


@dataclass(frozen=True)
class _Viewer:
    is_admin: bool
    group_ids: list[str]


class TriggerIntegrationVisibility:
    """Whether acting users can see trigger integrations, memoized.

    One instance serves one poll tick or one request: each acting user's admin
    flag and groups are read once, and each (user, integration) pair is looked
    up once, however many assignments or flow nodes share it.
    """

    def __init__(self) -> None:
        self._viewers: dict[uuid.UUID, _Viewer] = {}
        self._answers: dict[tuple[uuid.UUID, str], bool] = {}

    async def _viewer(self, db: AsyncSession, user_id: uuid.UUID) -> _Viewer:
        viewer = self._viewers.get(user_id)
        if viewer is None:
            from app.api.v1.helpers import get_user_group_ids

            user = await db.get(User, user_id)
            viewer = _Viewer(
                is_admin=bool(user is not None and user.is_admin),
                group_ids=await get_user_group_ids(db, user_id),
            )
            self._viewers[user_id] = viewer
        return viewer

    async def visible_ids(
        self, db: AsyncSession, acting_user_id: Any, integration_ids: Iterable[Any]
    ) -> set[str]:
        """The canonical ids among *integration_ids* the acting user can see.

        An id naming no integration is not visible. With no acting user, only
        the caller decides what applies (owner-less work keeps its rule), so
        nothing is returned.
        """
        from app.api.v1.helpers import user_visible_filter

        acting = normalize_acting_user_id(acting_user_id)
        parsed = {str(p) for p in (safe_uuid(str(i)) for i in integration_ids if i) if p}
        if acting is None or not parsed:
            return set()
        unknown = {i for i in parsed if (acting, i) not in self._answers}
        if unknown:
            viewer = await self._viewer(db, acting)
            result = await db.execute(
                select(Integration).where(
                    Integration.id.in_([uuid.UUID(i) for i in unknown]),
                    user_visible_filter(
                        Integration, acting, viewer.group_ids, is_admin=viewer.is_admin
                    ),
                )
            )
            seen = {str(row.id) for row in result.scalars().all()}
            for integration_id in unknown:
                self._answers[(acting, integration_id)] = integration_id in seen
        return {i for i in parsed if self._answers[(acting, i)]}

    async def sees(self, db: AsyncSession, acting_user_id: Any, integration_id: Any) -> bool:
        """Whether the acting user can see the integration *integration_id* names."""
        return bool(await self.visible_ids(db, acting_user_id, [integration_id]))

    async def hides(self, db: AsyncSession, acting_user_id: Any, integration_id: Any) -> bool:
        """Whether *integration_id* names an existing integration the acting user
        cannot see. An id naming nothing hides nothing."""
        if await self.sees(db, acting_user_id, integration_id):
            return False
        return bool(await _existing_ids(db, [integration_id]))


def _first_in_interval(key: tuple[str, str]) -> bool:
    """Whether *key* was not logged in the last :data:`UNSEEN_LOG_INTERVAL_SECONDS`
    in this process; if so, it counts as logged now."""
    now = time.monotonic()
    last = _last_unseen_log.get(key)
    if last is not None and now - last < UNSEEN_LOG_INTERVAL_SECONDS:
        return False
    if len(_last_unseen_log) >= _MAX_TRACKED_TARGETS:
        _last_unseen_log.clear()
    _last_unseen_log[key] = now
    return True


def log_unseen_trigger_integration(
    kind: str,
    target: Any,
    *,
    integration_id: Any,
    acting_user_id: Any,
) -> bool:
    """Log, once per *target* and :data:`UNSEEN_LOG_INTERVAL_SECONDS` in this
    process, that its events are delivered although its acting user cannot see
    its trigger integration. Returns whether the line was written. The pollers
    run on the scheduler's tick, so that is about one line per target and hour
    for the whole deployment; the security audit lists the same targets on
    demand.

    Only ids are named: the integration's configuration and credentials never
    reach the log.
    """
    if not _first_in_interval((kind, str(target))):
        return False
    logger.warning(
        "Integration events are delivered to %s %s, although the user it acts as "
        "(%s) cannot see its trigger integration %s. Share the integration with "
        "that user, or have the trigger name an integration they can see.",
        kind,
        target,
        acting_user_id,
        integration_id,
    )
    return True


async def _existing_ids(db: AsyncSession, integration_ids: Iterable[Any]) -> set[str]:
    parsed = {p for p in (safe_uuid(str(i)) for i in integration_ids if i) if p}
    if not parsed:
        return set()
    result = await db.execute(select(Integration).where(Integration.id.in_(parsed)))
    return {str(row.id) for row in result.scalars().all()}


@dataclass(frozen=True)
class TriggerVisibilityReport:
    """What the security audit reads about one kind of trigger (or sync source).

    *checked*: how many assignments (flows, sync sources) read an existing
    integration for an acting user. *unseen*: the ``(id, name)`` of those
    whose acting user cannot see it, sorted by name.
    """

    checked: int
    unseen: list[tuple[Any, str]]


async def _report(
    db: AsyncSession, targets: list[tuple[Any, str, uuid.UUID, str]]
) -> TriggerVisibilityReport:
    """*targets* are ``(id, name, acting, integration)``; a target whose
    integration no longer exists is not counted."""
    existing = await _existing_ids(db, (integration for *_rest, integration in targets))
    by_user: dict[uuid.UUID, set[str]] = {}
    for _id, _name, acting, integration in targets:
        if integration in existing:
            by_user.setdefault(acting, set()).add(integration)
    visibility = TriggerIntegrationVisibility()
    visible = {
        acting: await visibility.visible_ids(db, acting, ids)
        for acting, ids in by_user.items()
    }
    checked = {target_id for target_id, _name, _acting, integration in targets if integration in existing}
    unseen = dict.fromkeys(
        (target_id, name)
        for target_id, name, acting, integration in targets
        if integration in existing and integration not in visible.get(acting, set())
    )
    return TriggerVisibilityReport(
        checked=len(checked),
        unseen=sorted(unseen, key=lambda row: (row[1] or "", str(row[0]))),
    )


async def assignment_trigger_visibility(db: AsyncSession) -> TriggerVisibilityReport:
    """The enabled integration-event assignments, and those whose acting user
    cannot see the integration their trigger reads."""
    assignments = (
        await db.execute(
            select(Assignment).where(
                Assignment.trigger_type == "integration_event",
                Assignment.enabled.is_(True),
            )
        )
    ).scalars().all()
    agent_ids = {wf.agent_id for wf in assignments if wf.agent_id}
    agents: dict[Any, Agent] = {}
    if agent_ids:
        rows = await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))
        agents = {agent.id: agent for agent in rows.scalars().all()}
    targets: list[tuple[Any, str, uuid.UUID, str]] = []
    for wf in assignments:
        agent = agents.get(wf.agent_id)
        if agent is None or not agent.enabled:
            continue
        acting = assignment_acting_user_id(wf, agent)
        raw = trigger_integration_id(wf.trigger_config, agent)
        parsed = safe_uuid(str(raw)) if raw else None
        if acting is not None and parsed is not None:
            targets.append((wf.id, wf.name, acting, str(parsed)))
    return await _report(db, targets)


async def flow_trigger_visibility(db: AsyncSession) -> TriggerVisibilityReport:
    """The active flows with an integration-event trigger node, and those whose
    owner cannot see a node's integration."""
    from app.models.flow import FLOW_STATUS_ACTIVE, Flow
    from app.services.flow_runner import find_trigger_nodes

    flows = (
        await db.execute(select(Flow).where(Flow.status == FLOW_STATUS_ACTIVE))
    ).scalars().all()
    targets: list[tuple[Any, str, uuid.UUID, str]] = []
    for flow in flows:
        owner = normalize_acting_user_id(flow.user_id)
        if owner is None:
            continue
        for node in find_trigger_nodes(flow.graph or {}, "trigger.integration_event"):
            raw = (node.get("config") or {}).get("integration_id")
            parsed = safe_uuid(str(raw)) if raw else None
            if parsed is not None:
                targets.append((flow.id, flow.name, owner, str(parsed)))
    return await _report(db, targets)


# -- knowledge base sync sources ------------------------------------

_SYNC_SOURCE_KIND = "kb_sync_source"


def sync_source_acting_user_id(source_user_id: Any, kb_user_id: Any) -> uuid.UUID | None:
    """The identity a KB sync source syncs as: its configuring user, else the
    knowledge base's owner, ``None`` for none."""
    return normalize_acting_user_id(source_user_id) or normalize_acting_user_id(kb_user_id)


def log_unseen_sync_source_integration(
    source_id: Any,
    *,
    integration_id: Any,
    acting_user_id: Any,
) -> bool:
    """Log, once per sync source and :data:`UNSEEN_LOG_INTERVAL_SECONDS` in this
    process, that it syncs with its bound integration although the user it
    syncs as cannot see it. Returns whether the line was written. Only ids are
    named, as for a trigger."""
    if not _first_in_interval((_SYNC_SOURCE_KIND, str(source_id))):
        return False
    logger.warning(
        "Knowledge base sync source %s syncs with its bound integration %s, although "
        "the user it syncs as (%s) cannot see it. Share the integration with that "
        "user, or bind the source to an integration they can see.",
        source_id,
        integration_id,
        acting_user_id,
    )
    return True


async def sync_source_integration_visibility(db: AsyncSession) -> TriggerVisibilityReport:
    """The enabled KB sync sources bound to an integration, and those whose user
    cannot see it, each named after its knowledge base and source type."""
    from app.models.knowledge_base import KBSyncSource, KnowledgeBase

    rows = await db.execute(
        select(
            KBSyncSource.id,
            KBSyncSource.source_type,
            KBSyncSource.user_id,
            KBSyncSource.integration_id,
            KnowledgeBase.name,
            KnowledgeBase.user_id,
        )
        .join(KnowledgeBase, KnowledgeBase.id == KBSyncSource.knowledge_base_id)
        .where(KBSyncSource.enabled.is_(True), KBSyncSource.integration_id.is_not(None))
    )
    targets: list[tuple[Any, str, uuid.UUID, str]] = []
    for source_id, source_type, source_user, integration_id, kb_name, kb_user in rows.all():
        acting = sync_source_acting_user_id(source_user, kb_user)
        if acting is not None:
            targets.append((source_id, f"{kb_name} ({source_type})", acting, str(integration_id)))
    return await _report(db, targets)
