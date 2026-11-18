# SPDX-License-Identifier: Apache-2.0

"""Deactivate and reactivate an account, with everything that belongs to it.

The single path every deactivation goes through (#122): the administrator's
toggle (``PUT /users/{id}/active``), SCIM ``active=false`` and SCIM DELETE, an
SSO lockout. Before it, deactivation flipped ``User.is_active`` and every
surface derived the rest at read time (authentication refused, private
automations not firing, A2A exposures not resolving), which left the account's
live work running, its timers armed, its shared and company-managed objects
with a departed owner and nobody told.

What :func:`deactivate_user` does, each step in the caller's transaction:

1. ``is_active`` false, ``deactivated_at`` / ``deactivation_source`` stamped,
   ``deprovisioned_at`` on a SCIM DELETE. Every web session, personal API key,
   MCP token and platform JWT already refuses an inactive account on its next
   request (``api/deps.py``, ``services/api_key_access.py``); the keys are
   also revoked outright (``revoked_at``) so a reactivation never brings a
   leaked key back.
2. Live work stopped: runs, background tasks, chat turns, runner commands,
   flow runs (``panic.stop_owner_live_work``). Armed timers cancelled,
   scheduled one-offs not yet run cancelled.
3. Private agents (owned, not shared, not company-managed) disabled with their
   web widget; their A2A exposures are revoked by ``a2a.hooks`` on the
   ``enabled`` change. Their assignments stop with them (an assignment of a
   disabled agent never fires) and keep their own switch for the restore.
4. Shared and company-managed agents, fleets, workspaces and integrations the
   account owns are transferred to the platform administrator: the
   ``ADMIN_EMAIL`` account when it is active, else the oldest active
   administrator. On a transferred agent, an assignment that runs as the
   departed account (``run_as_user_id``) is paused
   (``owner_deactivated``); the others keep running as the new owner. Each
   transfer is listed for review.
5. Personal integrations disconnected: the account's own credentials on
   ``per_user`` integrations deleted; its private OAuth integrations' tokens
   cleared. Another person's agent bound to one of them is listed for review.
6. Open work items claimed by the account's runs released; shared-state
   leases held by its runs released (the end of a run already does both; the
   cascade stops the runs, so it calls the same sweeps).
7. Group memberships kept for audit: an inactive account resolves nothing.
8. One ``user_deactivations`` row (snapshot + review list + counts) and one
   audit row per step. A second deactivation of an inactive account is a
   no-op: the open episode is answered.
9. Every active administrator gets a notification-bell entry and an email.

:func:`reactivate_user` reverses 1 and 3 and the assignment pauses of 4 from
the snapshot, except an object an administrator reviewed or that changed owner
since; the keys, tokens, timers, one-offs and stopped runs stay ended. Both
flush, never commit: the caller commits, then awaits
:func:`after_commit` for what may only follow a durable change.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.api_key import UserApiKey
from app.models.assignment import Assignment
from app.models.fleet import Fleet
from app.models.integration import Integration
from app.models.integration_user_credential import IntegrationUserCredential
from app.models.platform_timer import TIMER_ARMED, TIMER_CANCELLED, PlatformTimer
from app.models.user import User, is_platform_admin
from app.models.user_deactivation import DEACTIVATION_SOURCES, UserDeactivation
from app.models.work_item import WorkItem
from app.models.workspace import Workspace
from app.services.assignment_pause import (
    OWNER_DEACTIVATED_REASON,
    clear_pause_provenance,
    mark_schedule_resumed,
    pause_assignment,
)

logger = logging.getLogger(__name__)

#: The cause every run, task and command the cascade cancels shows.
STOP_MESSAGE = "Stopped: the account it runs as was deactivated."
#: ``notification_source`` of the administrators' bell entries.
NOTIFICATION_SOURCE = "user_deactivation"
#: The audit actor of a cascade nobody signed in for (SCIM, SSO, the system).
SYSTEM_ACTOR = "user_deactivation"
#: Where the review list lives in the web app (S3 adds the page; until then the
#: user's own page).
REVIEW_PATH = "/settings/users"

#: Review ``action`` values: what the cascade did to the object.
ACTION_DISABLED = "disabled"
ACTION_TRANSFERRED = "transferred"
ACTION_PAUSED = "paused"
ACTION_DISCONNECTED = "disconnected"
ACTION_BOUND_ELSEWHERE = "bound_by_other_agent"
ACTION_KEPT = "kept_no_successor"

#: Review ``resolution`` values an administrator sets (S3).
RESOLUTIONS = ("keep", "reassign", "delete")


class DeactivationError(ValueError):
    """A deactivation the platform refuses: the reason is the message."""


@dataclass
class DeactivationOutcome:
    """What one call did, for the response, the audit row and the notification."""

    user_id: uuid.UUID
    episode_id: uuid.UUID | None = None
    already: bool = False
    successor_id: uuid.UUID | None = None
    counts: dict[str, int] = field(default_factory=dict)
    review_items: list[dict[str, Any]] = field(default_factory=list)
    stop: Any = field(default=None, repr=False)
    notify_admin_ids: list[uuid.UUID] = field(default_factory=list, repr=False)

    def add(self, key: str, value: int = 1) -> None:
        if value:
            self.counts[key] = self.counts.get(key, 0) + value

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": str(self.user_id),
            "deactivation_id": str(self.episode_id) if self.episode_id else None,
            "already_deactivated": self.already,
            "successor_user_id": str(self.successor_id) if self.successor_id else None,
            "counts": dict(self.counts),
            "review_count": len(self.review_items),
        }


def _label(user: User | None) -> str:
    if user is None:
        return "an administrator"
    return (user.full_name or "").strip() or user.email


def _review_item(kind: str, obj_id: Any, name: str | None, action: str, detail: str) -> dict[str, Any]:
    return {
        "key": f"{kind}:{obj_id}:{action}",
        "kind": kind,
        "id": str(obj_id),
        "name": name or str(obj_id),
        "action": action,
        "detail": detail,
        "reviewed_at": None,
        "reviewed_by": None,
        "resolution": None,
    }


async def resolve_successor(db: AsyncSession, departing: User) -> User | None:
    """The administrator who takes over what *departing* leaves behind.

    The ``ADMIN_EMAIL`` account when it is active and is not *departing*; else
    the oldest active administrator other than *departing*; else ``None`` (the
    objects stay where they are and are listed for review).
    """
    admins = (
        (
            await db.execute(
                select(User)
                .where(User.role == "admin", User.is_active.is_(True), User.id != departing.id)
                .order_by(User.created_at.asc(), User.id.asc())
            )
        )
        .scalars()
        .all()
    )
    for admin in admins:
        if is_platform_admin(admin):
            return admin
    return admins[0] if admins else None


async def open_episode(db: AsyncSession, user_id: uuid.UUID) -> UserDeactivation | None:
    return (
        await db.execute(
            select(UserDeactivation).where(
                UserDeactivation.user_id == user_id, UserDeactivation.reactivated_at.is_(None)
            )
        )
    ).scalar_one_or_none()


def _shared(row: Any) -> bool:
    return bool(getattr(row, "shared_group_ids", None) or getattr(row, "shared_user_ids", None))


def _private(row: Any) -> bool:
    return not getattr(row, "is_company_managed", False) and not _shared(row)


async def _free_slug(db: AsyncSession, model: Any, owner_id: uuid.UUID, slug: str) -> str:
    """A slug the new owner does not hold yet: *slug*, else ``slug-2``, ``slug-3``..."""
    taken = set(
        (await db.execute(select(model.slug).where(model.user_id == owner_id, model.slug.like(f"{slug}%"))))
        .scalars()
        .all()
    )
    if slug not in taken:
        return slug
    n = 2
    while f"{slug}-{n}" in taken:
        n += 1
    return f"{slug}-{n}"[:200]


# ---------------------------------------------------------------------------
# Deactivation
# ---------------------------------------------------------------------------


async def deactivate_user(
    db: AsyncSession,
    user: User,
    *,
    source: str,
    actor: User | None = None,
    actor_label: str | None = None,
    deprovision: bool = False,
    now: datetime | None = None,
) -> DeactivationOutcome:
    """Deactivate *user* and everything that belongs to it or acts as it.

    *source* is one of :data:`~app.models.user_deactivation.DEACTIVATION_SOURCES`;
    *actor* the administrator who acted (``None`` for SCIM, SSO, the system),
    *actor_label* how the actor is shown (``SCIM (<token name>)``).
    *deprovision* marks the account removed by the identity provider (SCIM
    DELETE) on top of the deactivation. Idempotent: an account already
    deactivated answers its open episode (``already``), only stamping
    ``deprovisioned_at`` when asked. Flushes, never commits.
    """
    if source not in DEACTIVATION_SOURCES:
        raise DeactivationError(f"unknown deactivation source {source!r}")
    if is_platform_admin(user):
        raise DeactivationError("The platform admin account cannot be disabled")
    if actor is not None and actor.id == user.id:
        raise DeactivationError("Cannot deactivate yourself")
    now = now or datetime.now(UTC)
    outcome = DeactivationOutcome(user_id=user.id)
    label = actor_label or (_label(actor) if actor is not None else source.upper())

    episode = await open_episode(db, user.id)
    if not user.is_active:
        outcome.already = True
        if episode is not None:
            outcome.episode_id = episode.id
            outcome.successor_id = episode.successor_user_id
            outcome.counts = dict(episode.counts or {})
            outcome.review_items = list(episode.review_items or [])
        if deprovision and user.deprovisioned_at is None:
            user.deprovisioned_at = now
            await db.flush()
        return outcome

    user.is_active = False
    user.deactivated_at = now
    user.deactivation_source = source
    if deprovision:
        user.deprovisioned_at = now
    await db.flush()

    snapshot: dict[str, Any] = {"agents": [], "assignments": [], "transfers": []}
    successor = await resolve_successor(db, user)
    outcome.successor_id = successor.id if successor is not None else None

    # 1. Credentials: revoke the personal API keys outright.
    keys = (
        await db.execute(
            update(UserApiKey)
            .where(UserApiKey.user_id == user.id, UserApiKey.revoked_at.is_(None))
            .values(revoked_at=now, is_active=False)
            .returning(UserApiKey.id)
        )
    ).all()
    outcome.add("api_keys_revoked", len(keys))

    # 2. Live work.
    from app.services.panic import stop_owner_live_work

    outcome.stop = await stop_owner_live_work(db, owner=user.id, message=STOP_MESSAGE, now=now)
    for name in (
        "runs_cancelled",
        "sub_agent_tasks_cancelled",
        "chat_turns_cancelled",
        "runner_commands_cancelled",
        "flow_runs_cancelled",
    ):
        outcome.add(name, getattr(outcome.stop, name, 0) or 0)
    timers = (
        await db.execute(
            update(PlatformTimer)
            .where(PlatformTimer.user_id == user.id, PlatformTimer.state == TIMER_ARMED)
            .values(state=TIMER_CANCELLED, ended_at=now, outcome=STOP_MESSAGE)
            .returning(PlatformTimer.id)
        )
    ).all()
    outcome.add("timers_cancelled", len(timers))
    one_offs = (
        await db.execute(
            update(WorkItem)
            .where(
                WorkItem.user_id == user.id,
                WorkItem.action_mode.is_not(None),
                WorkItem.status.in_(("open", "blocked")),
            )
            .values(
                status="cancelled",
                result=f"Cancelled on {now:%Y-%m-%d %H:%M} UTC, before it ran: the account was deactivated.",
                claimed_by_agent_id=None,
                claimed_at=None,
                last_activity_at=now,
            )
            .returning(WorkItem.id)
        )
    ).all()
    outcome.add("scheduled_actions_cancelled", len(one_offs))

    # 3 and 4. Agents.
    agents = (await db.execute(select(Agent).where(Agent.user_id == user.id))).scalars().all()
    for agent in agents:
        if _private(agent):
            if agent.enabled or agent.web_enabled:
                snapshot["agents"].append(
                    {"id": str(agent.id), "enabled": bool(agent.enabled), "web_enabled": bool(agent.web_enabled)}
                )
                agent.enabled = False
                agent.web_enabled = False
                outcome.add("private_agents_disabled")
            continue
        await _transfer_agent(db, agent, user, successor, outcome, snapshot, now)

    # Fleets and workspaces: private ones stay with the account (they hold
    # nothing that runs); shared and company-managed ones go to the successor.
    for model, kind in ((Fleet, "fleet"), (Workspace, "workspace")):
        rows = (await db.execute(select(model).where(model.user_id == user.id))).scalars().all()
        for row in rows:
            if _private(row):
                continue
            await _transfer_row(db, model, kind, row, user, successor, outcome, snapshot)

    # 5. Integrations.
    await _disconnect_integrations(db, user, successor, outcome, snapshot, now)

    # 6. Claims and leases of the runs stopped above (best effort: the tick
    # sweeps release them anyway).
    await db.flush()
    await _release_holds(outcome)

    # 8. The record and the audit rows.
    episode = UserDeactivation(
        user_id=user.id,
        source=source,
        actor_user_id=actor.id if actor is not None else None,
        actor_label=label[:320],
        successor_user_id=outcome.successor_id,
        snapshot=snapshot,
        review_items=outcome.review_items,
        counts=outcome.counts,
        deactivated_at=now,
    )
    db.add(episode)
    await db.flush()
    outcome.episode_id = episode.id
    await _audit(
        db,
        user,
        actor=actor,
        actor_label=label,
        action="deactivate",
        summary=f"Deactivated user '{user.email}' ({source}): "
        + (", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(outcome.counts.items())) or "nothing else to do"),
        details={
            "source": source,
            "deprovisioned": deprovision,
            "deactivation_id": str(episode.id),
            "successor_user_id": str(outcome.successor_id) if outcome.successor_id else None,
            "counts": outcome.counts,
            "review_items": [{k: i[k] for k in ("kind", "id", "name", "action")} for i in outcome.review_items],
        },
    )

    # 9. The administrators.
    outcome.notify_admin_ids = await _notify_admins(db, user, label, source, outcome)
    return outcome


async def _transfer_agent(
    db: AsyncSession,
    agent: Agent,
    departing: User,
    successor: User | None,
    outcome: DeactivationOutcome,
    snapshot: dict[str, Any],
    now: datetime,
) -> None:
    tier = "company-managed" if agent.is_company_managed else "shared"
    if successor is None:
        outcome.review_items.append(
            _review_item(
                "agent",
                agent.id,
                agent.name,
                ACTION_KEPT,
                f"{tier.capitalize()} agent; no other active administrator to take it over.",
            )
        )
        return
    agent.user_id = successor.id
    # The assignments moved with the agent are recorded so that a reactivation
    # takes back only those, never one the successor already owned.
    moved: list[str] = []
    snapshot["transfers"].append({"table": "agents", "id": str(agent.id), "assignments": moved})
    outcome.add("agents_transferred")
    # An assignment that would keep acting as the departed account is paused;
    # the others move to the new owner and keep running.
    assignments = (await db.execute(select(Assignment).where(Assignment.agent_id == agent.id))).scalars().all()
    paused = 0
    for assignment in assignments:
        acts_as_departed = assignment.run_as_user_id == departing.id
        if assignment.user_id == departing.id:
            assignment.user_id = successor.id
            moved.append(str(assignment.id))
        if acts_as_departed and assignment.enabled:
            pause_assignment(
                assignment,
                reason=OWNER_DEACTIVATED_REASON,
                details=f"It runs as {departing.email}, whose account was deactivated.",
                actor_label=SYSTEM_ACTOR,
                now=now,
            )
            snapshot["assignments"].append(str(assignment.id))
            paused += 1
            outcome.review_items.append(
                _review_item(
                    "assignment",
                    assignment.id,
                    assignment.name,
                    ACTION_PAUSED,
                    f"Of agent '{agent.name}'; it runs as {departing.email}.",
                )
            )
    outcome.add("assignments_paused", paused)
    outcome.review_items.append(
        _review_item(
            "agent",
            agent.id,
            agent.name,
            ACTION_TRANSFERRED,
            f"{tier.capitalize()} agent moved to {_label(successor)}"
            + (f"; {paused} assignment(s) running as the departed account paused." if paused else "."),
        )
    )


async def _transfer_row(
    db: AsyncSession,
    model: Any,
    kind: str,
    row: Any,
    departing: User,
    successor: User | None,
    outcome: DeactivationOutcome,
    snapshot: dict[str, Any],
) -> None:
    tier = "company-managed" if getattr(row, "is_company_managed", False) else "shared"
    if successor is None:
        outcome.review_items.append(
            _review_item(
                kind,
                row.id,
                row.name,
                ACTION_KEPT,
                f"{tier.capitalize()} {kind}; no other active administrator to take it over.",
            )
        )
        return
    record = {"table": model.__tablename__, "id": str(row.id)}
    slug = getattr(row, "slug", None)
    if slug is not None:
        new_slug = await _free_slug(db, model, successor.id, slug)
        if new_slug != slug:
            record["slug"] = slug
            row.slug = new_slug
    if getattr(row, "is_default", False):
        # The successor keeps its own default workspace.
        record["is_default"] = True
        row.is_default = False
    row.user_id = successor.id
    snapshot["transfers"].append(record)
    outcome.add(f"{kind}s_transferred")
    outcome.review_items.append(
        _review_item(
            kind, row.id, row.name, ACTION_TRANSFERRED, f"{tier.capitalize()} {kind} moved to {_label(successor)}."
        )
    )


async def _disconnect_integrations(
    db: AsyncSession,
    user: User,
    successor: User | None,
    outcome: DeactivationOutcome,
    snapshot: dict[str, Any],
    now: datetime,
) -> None:
    # The account's own credentials on per_user integrations.
    removed = (
        await db.execute(
            delete(IntegrationUserCredential)
            .where(IntegrationUserCredential.user_id == user.id)
            .returning(IntegrationUserCredential.integration_id)
        )
    ).all()
    outcome.add("personal_credentials_disconnected", len(removed))

    owned = (await db.execute(select(Integration).where(Integration.user_id == user.id))).scalars().all()
    disconnected: list[Integration] = []
    for integration in owned:
        if getattr(integration, "is_builtin", False):
            continue
        if _private(integration):
            # Written, never read: the token is cleared whatever it holds
            # (tests/test_integration_credential_gate.py counts any read).
            was_connected = integration.status != "disconnected"
            integration.access_token_enc = None
            integration.refresh_token_enc = None
            integration.status = "disconnected"
            if was_connected:
                disconnected.append(integration)
            continue
        await _transfer_row(db, Integration, "integration", integration, user, successor, outcome, snapshot)
        if successor is not None:
            from app.services.integration_credentials import record_token_owner_on_transfer

            record_token_owner_on_transfer(integration, user.id)
    outcome.add("integrations_disconnected", len(disconnected))
    for integration in disconnected:
        outcome.review_items.append(
            _review_item(
                "integration",
                integration.id,
                integration.name,
                ACTION_DISCONNECTED,
                f"{integration.provider} account of the departed user; its token was cleared.",
            )
        )
    # Another person's agent bound to one of the account's private integrations
    # loses those tools: list it.
    private_ids = {str(i.id) for i in owned if _private(i)}
    if not private_ids:
        return
    others = (
        (
            await db.execute(
                select(Agent).where(
                    or_(Agent.user_id.is_(None), Agent.user_id != user.id), Agent.integration_bindings.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    for agent in others:
        bound = {str(i) for ids in (agent.integration_bindings or {}).values() if isinstance(ids, list) for i in ids}
        hit = sorted(bound & private_ids)
        if hit:
            outcome.review_items.append(
                _review_item(
                    "agent",
                    agent.id,
                    agent.name,
                    ACTION_BOUND_ELSEWHERE,
                    f"Bound to {len(hit)} integration(s) of the departed user, now disconnected.",
                )
            )
            outcome.add("other_agents_bound")


async def _release_holds(outcome: DeactivationOutcome) -> None:
    # ``partner_cancel_run_ids`` is every run the stop cancelled, the trees
    # under the roots included (``panic._cancel_runs``).
    run_ids = list(getattr(outcome.stop, "partner_cancel_run_ids", None) or [])
    if not run_ids:
        return
    try:
        from app.services.work_item import release_ended_run_claims

        outcome.add("work_items_released", await release_ended_run_claims(run_ids=run_ids) or 0)
    except Exception as exc:  # the tick sweep releases them anyway
        logger.warning("Work item claims of a deactivated account not released now: %s", exc)
    try:
        from app.services.shared_state import release_ended_run_holds

        outcome.add("leases_released", await release_ended_run_holds(run_ids=run_ids) or 0)
    except Exception as exc:
        logger.warning("Shared state holds of a deactivated account not released now: %s", exc)


async def _audit(
    db: AsyncSession,
    user: User,
    *,
    actor: User | None,
    actor_label: str,
    action: str,
    summary: str,
    details: dict[str, Any],
) -> None:
    from app.services.audit import log_audit, log_system_audit

    try:
        if actor is not None:
            await log_audit(
                db,
                user=actor,
                action=action,
                entity_type="user",
                entity_id=user.id,
                entity_name=user.full_name,
                summary=summary,
                details=details,
            )
        else:
            await log_system_audit(
                db,
                actor=f"{SYSTEM_ACTOR}:{actor_label}"[:200],
                anchor_user_id=user.id,
                action=action,
                entity_type="user",
                entity_id=user.id,
                entity_name=user.full_name,
                summary=summary,
                details=details,
            )
    except Exception as exc:
        logger.warning("User %s audit row not written: %s", action, exc)


async def _notify_admins(
    db: AsyncSession, user: User, actor_label: str, source: str, outcome: DeactivationOutcome
) -> list[uuid.UUID]:
    """One bell entry per active administrator; the emails go after the commit."""
    admin_ids = (
        (await db.execute(select(User.id).where(User.role == "admin", User.is_active.is_(True)))).scalars().all()
    )
    pending = len(outcome.review_items)
    title = (
        f"{_label(user)} was deactivated by {actor_label}: {pending} object(s) to review"
        if pending
        else f"{_label(user)} was deactivated by {actor_label}"
    )
    summary = _summary_text(outcome)
    try:
        from app.services.notify import notify_web_user

        for admin_id in admin_ids:
            await notify_web_user(
                db,
                str(admin_id),
                title=title,
                summary=summary,
                source_label="Users",
                extra_data={
                    "notification_source": NOTIFICATION_SOURCE,
                    "user_id": str(user.id),
                    "deactivation_id": str(outcome.episode_id) if outcome.episode_id else None,
                    "source": source,
                    "review_count": pending,
                    "path": f"/users/{user.id}",
                },
            )
    except Exception as exc:
        logger.warning("Deactivation notice not delivered to the bell: %s", exc)
    return list(admin_ids)


def _summary_text(outcome: DeactivationOutcome) -> str:
    c = outcome.counts
    parts = [
        f"{c[k]} {label}"
        for k, label in (
            ("private_agents_disabled", "private agent(s) disabled"),
            ("agents_transferred", "agent(s) transferred"),
            ("assignments_paused", "assignment(s) paused"),
            ("fleets_transferred", "fleet(s) transferred"),
            ("workspaces_transferred", "workspace(s) transferred"),
            ("integrations_transferred", "integration(s) transferred"),
            ("integrations_disconnected", "integration(s) disconnected"),
            ("api_keys_revoked", "API key(s) revoked"),
            ("runs_cancelled", "run(s) stopped"),
            ("timers_cancelled", "timer(s) cancelled"),
            ("scheduled_actions_cancelled", "scheduled action(s) cancelled"),
        )
        if c.get(k)
    ]
    return ("; ".join(parts) + ".") if parts else "Nothing else belonged to the account."


async def after_commit(db_factory: Any, user: User, outcome: DeactivationOutcome, actor_label: str) -> None:
    """What may only follow a committed deactivation: partners, streams, emails. Never raises."""
    if outcome.already:
        return
    if outcome.stop is not None:
        try:
            from app.services.panic import announce_stopped_work

            await announce_stopped_work(outcome.stop)
        except Exception as exc:
            logger.warning("Stopped work of a deactivated account not announced: %s", exc)
    await _email_admins(db_factory, user, outcome, actor_label)


async def _email_admins(db_factory: Any, user: User, outcome: DeactivationOutcome, actor_label: str) -> None:
    try:
        from app.services.email import (
            email_button,
            email_heading,
            email_paragraph,
            is_email_configured,
            platform_url,
            render_email,
            send_email,
        )

        if not is_email_configured() or not outcome.notify_admin_ids:
            return
        async with db_factory() as db:
            emails = (await db.execute(select(User.email).where(User.id.in_(outcome.notify_admin_ids)))).scalars().all()
        pending = len(outcome.review_items)
        subject = f"{_label(user)} was deactivated" + (f": {pending} object(s) to review" if pending else "")
        body = (
            email_heading(subject)
            + email_paragraph(f"{_label(user)} ({user.email}) was deactivated by {actor_label}.")
            + email_paragraph(_summary_text(outcome))
            + email_button("Review", f"{platform_url()}/users/{user.id}")
        )
        html = render_email(title=subject, preheader=_summary_text(outcome), body_html=body)
        for to in emails:
            await send_email(to=to, subject=subject, html=html, text=f"{subject}\n\n{_summary_text(outcome)}")
    except Exception as exc:
        logger.warning("Deactivation email to administrators not sent: %s", exc)


# ---------------------------------------------------------------------------
# Reactivation
# ---------------------------------------------------------------------------


@dataclass
class ReactivationOutcome:
    user_id: uuid.UUID
    already: bool = False
    counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"user_id": str(self.user_id), "already_active": self.already, "counts": dict(self.counts)}


def _reviewed(episode: UserDeactivation, kind: str, obj_id: str) -> bool:
    return any(
        i.get("kind") == kind and i.get("id") == obj_id and i.get("reviewed_at") for i in (episode.review_items or [])
    )


async def reactivate_user(
    db: AsyncSession,
    user: User,
    *,
    source: str,
    actor: User | None = None,
    actor_label: str | None = None,
    now: datetime | None = None,
) -> ReactivationOutcome:
    """Reactivate *user* and put back what its deactivation switched off.

    Restored from the episode's snapshot: the private agents' ``enabled`` and
    web widget, the assignments paused ``owner_deactivated`` (only those still
    carrying that stamp), and the transfers (only an object still owned by the
    successor and not reviewed). Not restored: revoked keys, cleared tokens,
    cancelled timers, one-offs and runs. Idempotent. Flushes, never commits.
    """
    now = now or datetime.now(UTC)
    outcome = ReactivationOutcome(user_id=user.id)
    if user.is_active:
        outcome.already = True
        return outcome
    label = actor_label or (_label(actor) if actor is not None else source.upper())
    user.is_active = True
    user.deactivated_at = None
    user.deactivation_source = None
    user.deprovisioned_at = None
    episode = await open_episode(db, user.id)
    if episode is not None:
        snap = episode.snapshot or {}
        for entry in snap.get("agents", []):
            agent = await db.get(Agent, uuid.UUID(entry["id"]))
            if agent is None or agent.user_id != user.id or _reviewed(episode, "agent", entry["id"]):
                continue
            agent.enabled = bool(entry.get("enabled"))
            agent.web_enabled = bool(entry.get("web_enabled"))
            outcome.counts["agents_restored"] = outcome.counts.get("agents_restored", 0) + 1
        models: dict[str, Any] = {
            "agents": Agent,
            "fleets": Fleet,
            "workspaces": Workspace,
            "integrations": Integration,
        }
        for entry in snap.get("transfers", []):
            model = models.get(entry.get("table"))
            if model is None:
                continue
            row = await db.get(model, uuid.UUID(entry["id"]))
            kind = entry["table"][:-1]
            if (
                row is None
                or episode.successor_user_id is None
                or row.user_id != episode.successor_user_id
                or _reviewed(episode, kind, entry["id"])
            ):
                continue
            if entry.get("slug"):
                row.slug = await _free_slug(db, model, user.id, entry["slug"])
            if entry.get("is_default"):
                has_default = (
                    await db.execute(
                        select(Workspace.id).where(Workspace.user_id == user.id, Workspace.is_default.is_(True))
                    )
                ).first()
                row.is_default = has_default is None
            row.user_id = user.id
            moved_ids = [uuid.UUID(value) for value in entry.get("assignments") or []]
            if model is Agent and moved_ids:
                await db.execute(
                    update(Assignment)
                    .where(
                        Assignment.id.in_(moved_ids),
                        Assignment.agent_id == row.id,
                        Assignment.user_id == episode.successor_user_id,
                    )
                    .values(user_id=user.id)
                )
            outcome.counts["transfers_restored"] = outcome.counts.get("transfers_restored", 0) + 1
        for assignment_id in snap.get("assignments", []):
            assignment = await db.get(Assignment, uuid.UUID(assignment_id))
            if assignment is None or assignment.paused_reason != OWNER_DEACTIVATED_REASON:
                continue
            assignment.enabled = True
            clear_pause_provenance(assignment)
            mark_schedule_resumed(assignment, now)
            outcome.counts["assignments_resumed"] = outcome.counts.get("assignments_resumed", 0) + 1
        episode.reactivated_at = now
    await db.flush()
    await _audit(
        db,
        user,
        actor=actor,
        actor_label=label,
        action="reactivate",
        summary=f"Reactivated user '{user.email}' ({source})",
        details={"source": source, "counts": outcome.counts, "deactivation_id": str(episode.id) if episode else None},
    )
    try:
        from app.services.notify import notify_web_user

        admin_ids = (
            (
                await db.execute(
                    select(User.id).where(User.role == "admin", User.is_active.is_(True), User.id != user.id)
                )
            )
            .scalars()
            .all()
        )
        for admin_id in admin_ids:
            await notify_web_user(
                db,
                str(admin_id),
                title=f"{_label(user)} was reactivated by {label}",
                summary="What the deactivation switched off and nobody reviewed since was restored.",
                source_label="Users",
                extra_data={
                    "notification_source": NOTIFICATION_SOURCE,
                    "user_id": str(user.id),
                    "path": f"/users/{user.id}",
                },
            )
    except Exception as exc:
        logger.warning("Reactivation notice not delivered: %s", exc)
    return outcome


async def resolve_review_item(
    db: AsyncSession, episode: UserDeactivation, key: str, *, resolution: str, admin: User, now: datetime | None = None
) -> dict[str, Any]:
    """Mark one review item done (``keep``, ``reassign``, ``delete``); the action itself is the caller's."""
    if resolution not in RESOLUTIONS:
        raise DeactivationError(f"unknown resolution {resolution!r}")
    items = [dict(i) for i in (episode.review_items or [])]
    for item in items:
        if item.get("key") == key:
            item["reviewed_at"] = (now or datetime.now(UTC)).isoformat()
            item["reviewed_by"] = str(admin.id)
            item["resolution"] = resolution
            episode.review_items = items
            await db.flush()
            return item
    raise DeactivationError("No such review item")


__all__ = [
    "DeactivationError",
    "DeactivationOutcome",
    "ReactivationOutcome",
    "after_commit",
    "deactivate_user",
    "open_episode",
    "reactivate_user",
    "resolve_review_item",
    "resolve_successor",
]
