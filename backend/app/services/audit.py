# SPDX-License-Identifier: Apache-2.0

"""Audit logging service – records user actions across the platform.

Usage:
    from app.services.audit import log_audit
    await log_audit(db, user=user, action="create", entity_type="agent",
                    entity_id=agent.id, entity_name=agent.name,
                    summary="Created agent 'Mail Desk'",
                    request=request)

Security events:
    from app.services.audit import log_security_event
    await log_security_event(
        db, user_id=user_id, action="security_alert",
        entity_type="security", summary="Prompt injection detected",
        details={"severity": "high", "reasons": ["jailbreak_marker"]},
    )
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.undeclared_proxy import report_undeclared_proxy
from app.core.utils import request_client_ip
from app.models.audit_log import AuditLog
from app.models.user import User

logger = logging.getLogger(__name__)


def _get_client_ip(request: Request | None) -> str | None:
    """Client IP for the audit trail, under the same trust model as rate limiting.

    A forwarded request that trust model cannot read is reported to the admin
    (``app.core.undeclared_proxy``), never recorded differently.
    """
    if request is None:
        return None
    report_undeclared_proxy(request)
    return request_client_ip(request)


async def log_audit(
    db: AsyncSession,
    *,
    user: User,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | str | None = None,
    entity_name: str | None = None,
    summary: str,
    details: dict[str, Any] | None = None,
    request: Request | None = None,
) -> AuditLog:
    """Create an audit log entry.

    Parameters
    ----------
    db : AsyncSession
        Active database session (will be flushed, not committed).
    user : User
        The user performing the action.
    action : str
        Action verb: create, update, delete, login, logout, toggle, trigger,
        configure, bind, unbind, etc.
    entity_type : str
        Entity type affected: agent, assignment, integration, knowledge_base,
        mcp_server, custom_tool, user, llm_provider, general_assistant,
        sso, api_key, template, etc.
    entity_id : UUID | str | None
        ID of the affected entity (None for global actions like login).
    entity_name : str | None
        Human-readable name of the entity.
    summary : str
        One-line human-readable description of what happened.
    details : dict | None
        Extra structured data (old/new values, config changes).
    request : Request | None
        FastAPI request object for IP extraction.
    """
    entry = audit_entry(
        user=user,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        summary=summary,
        details=details,
        request=request,
    )
    db.add(entry)
    await db.flush()
    logger.debug("Audit: [%s] %s %s – %s", user.email, action, entity_type, summary)
    return entry


def audit_entry(
    *,
    user: User,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | str | None = None,
    entity_name: str | None = None,
    summary: str,
    details: dict[str, Any] | None = None,
    request: Request | None = None,
) -> AuditLog:
    """The row :func:`log_audit` writes, unsaved: for a caller that adds many
    in one flush (the emergency stop pausing every assignment under a lock)."""
    return AuditLog(
        user_id=user.id,
        user_email=user.email,
        action=action,
        entity_type=entity_type,
        entity_id=uuid.UUID(str(entity_id)) if entity_id else None,
        entity_name=entity_name,
        summary=summary,
        details=details,
        ip_address=_get_client_ip(request),
    )


#: ``AuditLog.user_email`` prefix of a row no human wrote.
SYSTEM_ACTOR_PREFIX = "system:"


async def log_system_audit(
    db: AsyncSession,
    *,
    actor: str,
    anchor_user_id: uuid.UUID | str,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | str | None = None,
    entity_name: str | None = None,
    summary: str,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Record an action the platform took on its own.

    ``AuditLog.user_id`` is NOT NULL, so the row is anchored on the user the
    action concerns (the owner of an automatically paused assignment), while
    ``user_email`` names the component that acted - ``system:<actor>`` - and
    ``details["actor"]`` repeats it: the anchor is who it happened to, never
    who did it. The same shape as the A2A auto-revocation rows
    (``a2a/hooks.py``).
    """
    system_actor = f"{SYSTEM_ACTOR_PREFIX}{actor}"
    entry = AuditLog(
        user_id=uuid.UUID(str(anchor_user_id)),
        user_email=system_actor,
        action=action,
        entity_type=entity_type,
        entity_id=uuid.UUID(str(entity_id)) if entity_id else None,
        entity_name=entity_name,
        summary=summary,
        details={**(details or {}), "actor": system_actor},
        ip_address=None,
    )
    db.add(entry)
    await db.flush()
    logger.debug("Audit: [%s] %s %s - %s", system_actor, action, entity_type, summary)
    return entry


async def log_security_event(
    db: AsyncSession,
    *,
    user_id: str,
    action: str,
    entity_type: str = "security",
    entity_id: uuid.UUID | str | None = None,
    entity_name: str | None = None,
    summary: str,
    details: dict[str, Any] | None = None,
    system_actor: str | None = None,
    anchor_agent_id: uuid.UUID | str | None = None,
) -> AuditLog | None:
    """Log a security event to the audit trail.

    Unlike :func:`log_audit`, this accepts a raw ``user_id`` string
    (which may be ``"anonymous"`` for unauthenticated channel users) and
    resolves the :class:`User` internally.  The extra DB lookup is
    acceptable because security events are rare.

    Returns the created :class:`AuditLog` entry, or ``None`` if the user
    could not be resolved (e.g. anonymous users).

    *system_actor* and *anchor_agent_id*, given together, keep the event of a
    ``user_id`` that is a UUID naming no account - the user was deleted while
    the work they started was still running: it is recorded through
    :func:`log_system_audit` under ``system:<system_actor>``, anchored on the
    owner of the agent *anchor_agent_id* names, the unresolved id kept as
    ``details["acting_user_id"]``. When that agent has no owner (a
    company-managed agent) or is gone, a WARNING names both ids, never the
    event's details. An id that is not a UUID is unchanged either way.
    """
    try:
        uid = uuid.UUID(user_id)
    except (ValueError, AttributeError):
        logger.info(
            "Security event for non-identifiable user (%s), skipping audit entry: %s",
            user_id,
            summary,
        )
        return None

    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if user is None:
        if system_actor is not None and anchor_agent_id is not None:
            return await _log_security_event_of_deleted_user(
                db,
                user_id=uid,
                system_actor=system_actor,
                anchor_agent_id=anchor_agent_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                entity_name=entity_name,
                summary=summary,
                details=details,
            )
        logger.warning(
            "Security event for unknown user_id %s, skipping audit entry: %s",
            user_id,
            summary,
        )
        return None

    return await log_audit(
        db,
        user=user,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        summary=summary,
        details=details,
    )


async def _log_security_event_of_deleted_user(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    system_actor: str,
    anchor_agent_id: uuid.UUID | str,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | str | None,
    entity_name: str | None,
    summary: str,
    details: dict[str, Any] | None,
) -> AuditLog | None:
    """The security event of a deleted user, anchored on the agent's owner."""
    from app.models.agent import Agent

    try:
        agent_uuid = uuid.UUID(str(anchor_agent_id))
    except ValueError:
        agent_uuid = None
    owner_id = None
    if agent_uuid is not None:
        owner_id = (
            await db.execute(
                select(User.id).join(Agent, Agent.user_id == User.id).where(Agent.id == agent_uuid)
            )
        ).scalar_one_or_none()
    if owner_id is None:
        logger.warning(
            "Security event for unknown user_id %s on agent %s, which has no owner to anchor "
            "it on, skipping audit entry: %s",
            user_id,
            anchor_agent_id,
            summary,
        )
        return None
    return await log_system_audit(
        db,
        actor=system_actor,
        anchor_user_id=owner_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        summary=summary,
        details={**(details or {}), "acting_user_id": str(user_id)},
    )
