# SPDX-License-Identifier: Apache-2.0

"""Invitation endpoints – admin-generated, email-bound, single-use links.

An admin generates an invitation for a specific email with a preset role. The
token lives in Redis with a TTL (auto-expires); only its hash is stored. The
returned link points at the frontend ``/register?token=...`` page. The invitee
registers with the email baked into the invite — it cannot be changed.
Consumption happens via ``POST /api/v1/auth/register-with-invitation``.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.database import get_db
from app.models.user import User
from app.services import token_store
from app.services.audit import log_audit

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateInvitationRequest(BaseModel):
    email: EmailStr
    role: str = "user"  # "admin" | "user"
    expires_in_hours: int | None = None
    # When False, no email is sent and the raw token + link are returned in the
    # response instead (for seed scripts / automation). Default True = the link
    # is emailed to the invitee and never returned.
    send_email: bool = True


class CreateInvitationResponse(BaseModel):
    id: str  # token hash (safe to expose; used for revoke)
    email: str
    role: str
    expires_at: str
    email_sent: bool
    # The registration link (token embedded) is returned ONLY when
    # send_email=False (seed/automation); otherwise it is emailed to the
    # invitee and never returned.
    link: str | None = None


class InvitationListItem(BaseModel):
    id: str
    email: str
    role: str
    status: str  # always "pending" — used/expired invites are gone from Redis
    created_at: str | None = None
    expires_at: str | None = None


class ValidateInvitationResponse(BaseModel):
    valid: bool
    email: str | None = None
    role: str | None = None
    expires_at: str | None = None


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------


@router.post("", response_model=CreateInvitationResponse, status_code=status.HTTP_201_CREATED)
async def create_invitation(
    body: CreateInvitationRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Generate a single-use, email-bound invitation link (admin-only)."""
    if body.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'user'")

    # ``is not None`` rather than ``or`` so an explicit 0 fails the range
    # check below instead of silently defaulting to the configured TTL.
    # Only a missing field (``None``) inherits the default (Settings >
    # Security, or INVITATION_EXPIRE_HOURS, which wins).
    from app.core.security_options import security_option

    hours = (
        body.expires_in_hours
        if body.expires_in_hours is not None
        else security_option("invitation_expire_hours")
    )
    if hours < 1 or hours > 720:
        raise HTTPException(
            status_code=400, detail="expires_in_hours must be between 1 and 720"
        )

    # Normalise the email for both the existing-user lookup and the
    # invitation-dedup lookup so the two checks agree on identity. Without
    # the ``func.lower`` here, a user registered as ``Foo@Example.com``
    # would slip past the exists check on a case-different invite (the
    # Postgres ``=`` operator is case-sensitive on text columns), while the
    # pending-invite lookup *did* normalise — leaving a window where the
    # invite is created and then ``register-with-invitation`` 409s on the
    # case-different existing user. Stripping aligns with how
    # ``find_invitation_by_email`` already normalises (``.strip().lower()``).
    target_email = str(body.email).strip().lower()
    exists = await db.execute(
        select(User).where(func.lower(User.email) == target_email)
    )
    if exists.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail="A user with this email already exists"
        )

    # Don't stack duplicate invitations: a valid pending invite already covers
    # this email. The admin can revoke it first if they want to resend.
    if await token_store.find_invitation_by_email(target_email):
        raise HTTPException(
            status_code=409,
            detail="A pending invitation already exists for this email. "
            "Revoke it before sending a new one.",
        )

    from app.services.email import invitation_email, is_email_configured, send_email

    # Refuse to create an invitation we can't deliver.  When the admin opts
    # into ``send_email=True`` (the UI default) but SMTP isn't configured,
    # silently writing the token to Redis would leave them with a pending
    # invite they can't recover — the link is never emailed and (in a
    # non-debug build) never logged either.  Failing fast with 503 makes the
    # misconfiguration visible: the admin either configures SMTP and retries,
    # or calls the API with ``send_email=false`` (seed/automation path) to
    # take the link directly.
    if body.send_email and not is_email_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Email delivery is not configured (SMTP_HOSTNAME is unset), so "
                "the invitation link cannot be sent. Configure SMTP and retry, "
                "or pass ``send_email=false`` to retrieve the link directly."
            ),
        )

    # Use the normalised ``target_email`` everywhere downstream (token
    # payload, email recipient, audit log, response body) so the value
    # admins see in the audit trail / pending list matches the canonical
    # form actually used for identity comparisons.  Without this, an
    # invite for ``Foo@Example.com`` would leave a payload + audit record
    # in mixed case while the issuance-time exists check ran against the
    # lowered form — confusing to operators chasing down a "why didn't
    # this invite collide with the existing account?" report later.
    raw_token, token_hash, expires_at = await token_store.create_invitation(
        email=target_email,
        role=body.role,
        created_by_id=admin.id,
        ttl_hours=hours,
    )

    link = token_store.invitation_link(raw_token)

    email_sent = False
    return_link: str | None = None
    if body.send_email:
        # ``send_email`` is best-effort once SMTP is configured: a transient
        # SMTP failure (auth, network, bad From: address, etc.) returns False
        # but doesn't raise here, so the invitation lives in Redis.  The
        # admin sees ``email_sent=false`` and can revoke + reissue from the
        # pending list — the dialog copy reflects this so they aren't told
        # the link "was logged on the server" (it isn't).
        # Subject uses the resolved branded name so the email metadata
        # (inbox preview line + notification text) matches the body and
        # the operator's custom branding — hard-coding "Filigran Copilot"
        # would show the wrong product to recipients on white-labelled
        # deployments, even though the body already renders the right
        # name via ``invitation_email`` → ``_brand_context``.
        from app.services.branding import get_cached_branding

        html, text = invitation_email(link, body.role, hours)
        email_sent = await send_email(
            to=target_email,
            subject=f"You've been invited to {get_cached_branding().name}",
            html=html,
            text=text,
        )
    else:
        # Admin opted out of email (seed scripts / automation): return the link.
        return_link = link

    await log_audit(
        db,
        user=admin,
        action="create",
        entity_type="invitation",
        entity_name=target_email,
        summary=f"Created {body.role} invitation for '{target_email}'"
        + ("" if body.send_email else " (token returned, email skipped)"),
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return CreateInvitationResponse(
        id=token_hash,
        email=target_email,
        role=body.role,
        expires_at=expires_at.isoformat(),
        email_sent=email_sent,
        link=return_link,
    )


@router.get("", response_model=list[InvitationListItem])
async def list_invitations(
    admin: User = Depends(require_admin),
):
    """List pending invitations (admin-only)."""
    items = await token_store.list_invitations()
    return [
        InvitationListItem(
            id=it["id"],
            email=it.get("email", ""),
            role=it.get("role", "user"),
            status="pending",
            created_at=it.get("created_at"),
            expires_at=it.get("expires_at"),
        )
        for it in items
    ]


@router.delete("/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invitation(
    invitation_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a pending invitation by id (admin-only)."""
    removed = await token_store.revoke_invitation(invitation_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Invitation not found")
    await log_audit(
        db,
        user=admin,
        action="delete",
        entity_type="invitation",
        summary="Revoked invitation link",
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()


# ---------------------------------------------------------------------------
# Public route (no auth) – used by the register page
# ---------------------------------------------------------------------------


@router.get("/validate/{token}", response_model=ValidateInvitationResponse)
async def validate_invitation(token: str):
    """Validate an invitation token; return its bound email + role (no auth)."""
    data = await token_store.get_invitation(token)
    if data is None:
        return ValidateInvitationResponse(valid=False)
    return ValidateInvitationResponse(
        valid=True,
        email=data.get("email"),
        role=data.get("role"),
        expires_at=data.get("expires_at"),
    )
