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

"""Persistence for standing approval preferences.

A standing approval ("Yes, always") is a *user preference*, not shared
configuration: it belongs to the person who granted it, follows them across all
of their runs including scheduled ones, and is never visible to or usable by
another user. Administrators get no cross-user view — acting on someone else's
preference goes through the platform's existing impersonation mechanism, which
keeps a single audited path rather than a parallel privileged surface.

A preference is consent to a tool's *shape*, not to its name: it records a
fingerprint of the tool's input schema so that a materially changed tool forces
re-approval instead of inheriting stale consent.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import get_or_create_counter
from app.models.tool_approval import ToolApprovalPreference

logger = logging.getLogger(__name__)

#: Lost audit writes for standing approvals. The write is best-effort by
#: design — failing a user's click to protect the record would be the wrong
#: trade — but "best-effort" must still be observable, or consent quietly
#: exists with no trail. Labelled by action so a grant going unrecorded is
#: distinguishable from a revocation.
_audit_write_failures = get_or_create_counter(
    "tool_approval_audit_write_failures_total",
    "Audit records for standing tool approvals that could not be written",
    ["action"],
)

REASON_USER_REVOKED = "user_revoked"
REASON_TOOL_CHANGED = "tool_changed"


class ApprovalPreferencesUnavailableError(RuntimeError):
    """Raised when a user's standing approvals cannot be read.

    Like the whitelist, this is deliberately not collapsed into "no approvals".
    An empty mapping means "this user has approved nothing", which causes tools
    to gate — safe. But a caller that cannot distinguish it from a failed read
    would silently treat a database blip as a policy decision, and the audit
    trail would record calls as gated-by-policy when they were really
    gated-by-outage.
    """


async def list_active_exceptions(
    db: AsyncSession,
    user_id: uuid.UUID | None,
    *,
    sources: dict[str, str] | None = None,
    entity_ids: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return ``{tool_key: schema_fingerprint}`` for one user's live approvals.

    *sources*, when given, is filled with ``{tool_key: tool_source}`` — which
    tool each approval was granted to, for
    :func:`app.tools.approval.standing_approval_covers`. *entity_ids*, when
    given, is filled with ``{tool_key: tool_entity_id}`` for the approvals that
    recorded one — for an MCP tool, the id of the server it was granted on.

    Resolve ONCE per run and reuse — this is consulted for every gated tool
    call.

    A run with no resolved user (owner-less company-managed runs are the real
    case) has no preferences by definition, so it gets an empty mapping and
    every gated tool prompts.
    """
    if user_id is None:
        return {}

    try:
        result = await db.execute(
            select(
                ToolApprovalPreference.tool_key,
                ToolApprovalPreference.schema_fingerprint,
                ToolApprovalPreference.tool_source,
                ToolApprovalPreference.tool_entity_id,
            ).where(
                ToolApprovalPreference.user_id == user_id,
                ToolApprovalPreference.revoked_at.is_(None),
            )
        )
        rows = result.all()
    except Exception as exc:
        logger.error("tool_approval_preferences_lookup_failed", exc_info=exc)
        raise ApprovalPreferencesUnavailableError(
            "Could not read standing approvals"
        ) from exc

    if sources is not None:
        sources.update({tool_key: source for tool_key, _fp, source, _id in rows})
    if entity_ids is not None:
        entity_ids.update(
            {tool_key: str(eid) for tool_key, _fp, _src, eid in rows if eid}
        )
    return {tool_key: fp for tool_key, fp, _source, _id in rows}


async def list_preferences(
    db: AsyncSession, user_id: uuid.UUID
) -> list[ToolApprovalPreference]:
    """Every active standing approval belonging to one user."""
    result = await db.execute(
        select(ToolApprovalPreference)
        .where(
            ToolApprovalPreference.user_id == user_id,
            ToolApprovalPreference.revoked_at.is_(None),
        )
        .order_by(ToolApprovalPreference.granted_at.desc())
    )
    return list(result.scalars().all())


async def revoke_exception(
    db: AsyncSession,
    *,
    preference_id: uuid.UUID,
    user_id: uuid.UUID,
    reason: str = REASON_USER_REVOKED,
) -> ToolApprovalPreference | None:
    """Revoke one of ``user_id``'s own approvals. ``None`` if it is not theirs.

    Scoped to the owner in the query itself rather than checked afterwards, so
    another user's preference is indistinguishable from a missing one — the
    caller returns 404 either way and never confirms that someone else's
    preference exists.

    Soft delete: the row stays so the audit trail survives, and so "why did this
    start prompting again?" stays answerable — revoked by you, or invalidated
    because the tool changed.
    """
    preference = await db.scalar(
        select(ToolApprovalPreference).where(
            ToolApprovalPreference.id == preference_id,
            ToolApprovalPreference.user_id == user_id,
            ToolApprovalPreference.revoked_at.is_(None),
        )
    )
    if preference is None:
        return None

    preference.revoked_at = datetime.now(UTC)
    preference.revoked_reason = reason
    return preference


async def list_recently_invalidated(
    db: AsyncSession, user_id: uuid.UUID, limit: int = 10
) -> list[ToolApprovalPreference]:
    """Approvals revoked because their tool changed, most recent first.

    Only ``tool_changed`` revocations, never ``user_revoked``: a user who
    revoked something themselves does not need it explained back to them, while
    a tool that started prompting again on its own does.
    """
    result = await db.execute(
        select(ToolApprovalPreference)
        .where(
            ToolApprovalPreference.user_id == user_id,
            ToolApprovalPreference.revoked_reason == REASON_TOOL_CHANGED,
        )
        .order_by(ToolApprovalPreference.revoked_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def invalidate_stale_approvals(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    tool_keys: set[str] | list[str],
    reason: str = REASON_TOOL_CHANGED,
) -> list[ToolApprovalPreference]:
    """Revoke approvals whose tool no longer matches what the user consented to.

    Called when the gate detects a fingerprint mismatch: the stored consent
    describes a different call shape than the tool now has, so it no longer
    means anything.

    Marking the row rather than leaving it is what keeps the user's view honest
    — otherwise their preference list claims they always allow a tool that is
    visibly prompting them, with nothing to explain the difference. It also
    prevents consent from coming back: an untouched row whose tool later
    reverted to its old schema would start matching again and silently resume,
    reviving an approval the user never re-gave.

    Writes an audit record per revocation so the change is attributable, and
    records ``tool_changed`` rather than ``user_revoked`` so "why is this asking
    me again?" stays answerable.
    """
    if not tool_keys:
        return []

    result = await db.execute(
        select(ToolApprovalPreference).where(
            ToolApprovalPreference.user_id == user_id,
            ToolApprovalPreference.tool_key.in_(list(tool_keys)),
            ToolApprovalPreference.revoked_at.is_(None),
        )
    )
    preferences = list(result.scalars().all())

    for preference in preferences:
        preference.revoked_at = datetime.now(UTC)
        preference.revoked_reason = reason
        logger.info(
            "tool_approval_invalidated",
            extra={
                "user_id": str(user_id),
                "tool_key": preference.tool_key,
                "reason": reason,
            },
        )
        # FR-019b: an invalidation is a change to a standing approval and
        # belongs in the same trail as granting and revoking one. A log line is
        # not that trail — the user sees the tool start prompting again, and
        # "why" has to be answerable from the record rather than from whoever
        # can read application logs.
        await _audit_preference_change(
            db,
            preference,
            action="update",
            summary=(
                f"Standing approval for '{preference.tool_key}' invalidated "
                "because the tool changed"
            ),
            reason=reason,
        )

    return preferences


async def grant_exception(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    tool_key: str,
    tool_source: str,
    schema_fingerprint: str,
    granted_by_admin_id: uuid.UUID | None = None,
    tool_entity_id: str | None = None,
) -> ToolApprovalPreference:
    """Record (or refresh) one user's standing approval for one tool.

    Upserts on the active ``(user_id, tool_key)`` pair so two concurrent
    "Yes, always" clicks converge on one row rather than racing to insert
    duplicates — the partial unique index would reject the second anyway, but
    converging is the behaviour a user expects.

    Re-granting a tool whose schema has since changed updates the fingerprint,
    which is the point: the user is consenting to the tool as it is *now*.

    ``granted_by_admin_id`` records an administrator acting through
    impersonation, so the audit trail names both identities rather than
    attributing the change solely to the impersonated account.

    ``tool_entity_id`` is ``ToolDefinition.entity_id`` of the approved tool. It
    is what lets an approval cover an MCP tool at all: the gate matches one only
    by its server's id. Re-granting the name on another server moves the
    approval there, and counts as fresh consent.
    """
    tool_entity_id = str(tool_entity_id) if tool_entity_id else None
    existing = await db.scalar(
        select(ToolApprovalPreference).where(
            ToolApprovalPreference.user_id == user_id,
            ToolApprovalPreference.tool_key == tool_key,
            ToolApprovalPreference.revoked_at.is_(None),
        )
    )
    if existing is not None:
        _reshaped = existing.schema_fingerprint != schema_fingerprint
        # Another connection's tool of that name — another MCP server's — is
        # another tool, so approving it is fresh consent too.
        _moved = existing.tool_entity_id != tool_entity_id
        _reconsented = _reshaped or _moved
        existing.schema_fingerprint = schema_fingerprint
        existing.tool_source = tool_source
        existing.tool_entity_id = tool_entity_id
        # Only ever set, never cleared. A caller that does not know about
        # impersonation passes None, and overwriting with it would erase the
        # administrator recorded on an earlier grant — losing the attribution
        # FR-017b exists to keep, on a row whose history is the whole point.
        if granted_by_admin_id is not None:
            existing.granted_by_admin_id = granted_by_admin_id
        # Re-granting a tool whose shape had changed is a fresh act of consent,
        # not a no-op refresh — the user is approving something they had not
        # approved before, so it belongs in the trail as much as a first grant.
        if _reconsented:
            await _audit_preference_change(
                db,
                existing,
                action="update",
                summary=(
                    f"Re-granted standing approval for '{tool_key}' after the "
                    "tool changed"
                    if _reshaped
                    else f"Re-granted standing approval for '{tool_key}' on "
                    "another connection"
                ),
                actor_admin_id=granted_by_admin_id,
            )
        return existing

    preference = ToolApprovalPreference(
        user_id=user_id,
        tool_key=tool_key,
        tool_source=tool_source,
        tool_entity_id=tool_entity_id,
        schema_fingerprint=schema_fingerprint,
        granted_by_admin_id=granted_by_admin_id,
    )
    db.add(preference)
    # Flushed before auditing so the record can name the row it describes; the
    # id is a Python-side default that does not exist until then.
    await db.flush()
    await _audit_preference_change(
        db,
        preference,
        action="create",
        summary=f"Granted standing approval for '{tool_key}'",
        actor_admin_id=granted_by_admin_id,
    )
    return preference


async def grant_chat_standing_approval(
    db: AsyncSession,
    *,
    user_id: object,
    proposal: dict,
    decision: dict | None,
    turn_impersonated_by: object,
) -> str | None:
    """Persist a chat turn's "Yes, always" and return the tool key granted.

    Granted to the identity the turn runs as (FR-008c) and attributed to
    whoever answered (FR-017b): the answer arrives on its own request, from
    any of the owner's sessions, so the turn's impersonation is only the
    fallback. Fingerprinted against the schema in the proposal, the call the
    reviewer was shown, as the run path does.
    """
    from app.tools.approval import approver_admin_id, fingerprint

    tool_key = proposal.get("tool_name")
    if not tool_key or not user_id:
        return None
    await grant_exception(
        db,
        user_id=uuid.UUID(str(user_id)),
        tool_key=tool_key,
        tool_source=proposal.get("source", "unknown"),
        # An MCP tool's grant covers the server it was granted on only.
        tool_entity_id=proposal.get("tool_entity_id"),
        schema_fingerprint=fingerprint(proposal.get("input_schema")),
        granted_by_admin_id=approver_admin_id(decision, turn_impersonated_by),
    )
    return tool_key


async def _audit_preference_change(
    db: AsyncSession,
    preference: ToolApprovalPreference,
    *,
    action: str,
    summary: str,
    actor_admin_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> None:
    """Write the audit record for one change to a standing approval (FR-018).

    Emitted from the service rather than from each caller, because that is the
    failure this closes: revocation was audited at its endpoint and granting
    was not, so the trail recorded consent being withdrawn but never given —
    and nothing about the code made the omission visible. A caller added later
    inherits the record instead of having to remember it.

    ``impersonated_by`` names the administrator when one acted through
    impersonation, so the trail carries both identities (FR-017b).

    Best-effort: a preference that could not be audited is still a preference,
    and failing the user's click to protect the record would be the wrong trade.
    The write is deliberately loud in the log when it fails.

    Wrapped in a SAVEPOINT to make that trade real. ``log_audit`` flushes, so a
    failure there leaves the session needing a rollback; swallowing the
    exception and carrying on would hand the caller a poisoned session and
    surface as ``PendingRollbackError`` on the *next* commit — turning "the
    audit row was lost" into "the chat turn failed", the exact inversion this
    docstring promises not to make. The savepoint discards only the audit write.
    """
    from app.models.user import User
    from app.services.audit import log_audit

    # Read before the try, and defensively: the handler below exists to swallow
    # failures, so anything it needs must already be in hand. Reaching back into
    # the object there could raise inside the very block meant to contain the
    # damage, turning a missing audit record into a failed approval.
    tool_key = getattr(preference, "tool_key", "<unknown>")

    try:
        # Falls back to the impersonator recorded on the row itself, so an
        # invalidation still names the administrator behind a grant they made
        # on someone else's behalf without every caller having to pass it.
        if actor_admin_id is None:
            actor_admin_id = getattr(preference, "granted_by_admin_id", None)
        user = await db.scalar(select(User).where(User.id == preference.user_id))
        if user is None:
            logger.warning(
                "No user for standing approval %s; audit record skipped", tool_key
            )
            return
        details = {
            "tool_key": tool_key,
            "tool_source": preference.tool_source,
            "impersonated_by": str(actor_admin_id) if actor_admin_id else None,
        }
        if reason:
            details["reason"] = reason

        _savepoint = getattr(db, "begin_nested", None)
        if _savepoint is None:
            # A stand-in session (tests) with no savepoint support; the write
            # cannot poison what it does not own.
            await log_audit(
                db,
                user=user,
                action=action,
                entity_type="tool_approval_preference",
                entity_id=getattr(preference, "id", None),
                entity_name=tool_key,
                summary=summary,
                details=details,
            )
        else:
            async with _savepoint():
                await log_audit(
                    db,
                    user=user,
                    action=action,
                    entity_type="tool_approval_preference",
                    entity_id=getattr(preference, "id", None),
                    entity_name=tool_key,
                    summary=summary,
                    details=details,
                )
    except Exception:
        # Alertable, not just greppable. ``grant_exception`` is the only writer
        # of consent, so a lost audit row means a standing approval exists with
        # no record of who granted it — on a row whose history is the point. A
        # log line nobody queries is not a detection.
        _audit_write_failures.labels(action=action).inc()
        logger.warning(
            "Could not audit a standing approval change for '%s'",
            tool_key,
            exc_info=True,
        )
