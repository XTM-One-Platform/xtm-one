# SPDX-License-Identifier: Apache-2.0

"""Model announcements from Settings > AI and models > Models.

Admins email every user "<model> is now your default model": right when they
save a new default (``announce_default_model`` on ``PUT /settings/general-assistant``)
or, with these routes, about the current default - which is also how an
env-managed default gets announced. A provider edit that adds or retires a model
the users pick from is recorded by the provider routes; the saves of the window
are offered here as one change the admin edits and emails when they choose to.
A test email to the admin comes first, and the email itself can be
previewed without sending anything; the models the configured providers serve
are listed for the admin to pick retired ones from. Delivery is the
scheduler's, as for the env announcements, and only while agentic AI
runs, so the writes here are gated the same way.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin, require_agentic_ai, require_ai_enabled
from app.api.v1.helpers import parse_path_id
from app.api.v1.platform_settings_helpers import peek_platform_settings
from app.api.v1.schemas.settings import (
    CapabilityNewsSummary,
    CatalogModelSummary,
    ModelAnnouncementCatalogsResponse,
    ModelAnnouncementPreviewResponse,
    ModelAnnouncementStatusResponse,
    ModelAnnouncementSummary,
    ModelAnnouncementTestRequest,
    ModelAnnouncementTestResponse,
    ModelCatalogChangeSummary,
    ModelChangesAnnouncementRequest,
    ProviderCatalogSummary,
    RetiredModelChoice,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import enforce_rate_limit
from app.models.user import User
from app.services import model_catalog_announcements as announcements
from app.services.audit import log_audit

router = APIRouter()

# A test email is a real SMTP send from a button: a handful per admin is plenty.
TEST_EMAIL_LIMIT = 5
TEST_EMAIL_WINDOW_SECONDS = 600
# Listing the provider catalogs asks every configured provider: the dialog caches
# the answer for 10 minutes, so this only stops a loop.
CATALOGS_LIMIT = 10
CATALOGS_WINDOW_SECONDS = 600
CHANGE_NOT_FOUND_DETAIL = "Model change not found"


def _summary(progress: announcements.AnnouncementProgress) -> ModelAnnouncementSummary:
    row, change = progress.row, progress.change
    return ModelAnnouncementSummary(
        id=str(row.id),
        source="admin" if row.source == announcements.SOURCE_ADMIN else "env",
        status=row.status,
        skip_reason=row.skip_reason,
        default_model=change.new_default.id if change.new_default else None,
        previous_default_model=change.previous_default,
        added_models=[model.id for model in change.added],
        removed_models=[model.id for model in change.removed],
        capabilities=[
            CapabilityNewsSummary(
                capability=item.capability,
                model=item.model,
                provider=item.provider,
                previous_model=item.previous_model,
            )
            for item in change.capabilities
        ],
        created_at=row.created_at.isoformat(),
        sent_at=row.sent_at.isoformat() if row.sent_at else None,
        delivered=progress.delivered,
        refused=progress.refused,
        unknown=progress.unknown,
        remaining=progress.remaining,
        queued_behind=progress.queued_behind,
        retry_at=progress.retry_at.isoformat() if progress.retry_at else None,
        failed_attempts=progress.failed_attempts,
        last_error=progress.last_error,
        held_until=progress.held_until.isoformat() if progress.held_until else None,
        blocked_until=progress.blocked_until.isoformat() if progress.blocked_until else None,
        deferred=progress.deferred,
        deferred_retry_at=progress.deferred_retry_at.isoformat() if progress.deferred_retry_at else None,
        deferred_retrying=progress.deferred_retrying,
        last_refusal=progress.last_refusal,
    )


def _models(models: Sequence[announcements.CatalogModel]) -> list[CatalogModelSummary]:
    return [CatalogModelSummary(id=model.id, provider=model.provider) for model in models]


def catalog_change_summary(draft: announcements.CatalogDraft) -> ModelCatalogChangeSummary:
    """The model changes saved and not emailed yet, as Settings offers to email them."""
    change = draft.change
    return ModelCatalogChangeSummary(
        id=str(draft.version.id),
        added_models=[model.id for model in change.added],
        removed_models=[model.id for model in change.removed],
        added=_models(change.added),
        removed=_models(change.removed),
        created_at=draft.version.created_at.isoformat(),
        first_saved_at=draft.first_saved_at.isoformat(),
        saves=draft.saves,
    )


def _change_details(change: announcements.CatalogChange) -> dict[str, list[str]]:
    return {
        "added_models": [model.id for model in change.added],
        "removed_models": [model.id for model in change.removed],
    }


@router.get("", response_model=ModelAnnouncementStatusResponse)
async def get_model_announcement_status(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> ModelAnnouncementStatusResponse:
    from app.services.email import is_email_configured

    ps = await peek_platform_settings(db)
    default_model = ps.agent_default_model if ps is not None else None
    latest = await announcements.latest_announcement(db)
    offered = await announcements.read_offered_models(db)
    draft = await announcements.catalog_draft(db, offered=offered)
    return ModelAnnouncementStatusResponse(
        email_configured=is_email_configured(),
        emails_disabled=announcements.emails_turned_off(),
        recipient_count=await announcements.announcement_recipient_count(db),
        emails_per_minute=settings.model_catalog_emails_batch_size,
        default_model=default_model,
        default_provider=ps.agent_default_provider if ps is not None else None,
        previous_default_model=await announcements.previous_default_for(db, default_model) if default_model else None,
        latest=_summary(latest) if latest is not None else None,
        unannounced_change=catalog_change_summary(draft) if draft is not None else None,
        changes_window_hours=settings.model_catalog_changes_window_hours,
        offered_models=_models(offered.models),
        formerly_offered_models=_models(await announcements.formerly_offered_models(db, offered)),
    )


@router.get("/catalogs", response_model=ModelAnnouncementCatalogsResponse)
async def list_model_catalogs(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
    _ai_gate: None = Depends(require_ai_enabled),
) -> ModelAnnouncementCatalogsResponse:
    """The chat models every configured provider serves that the users cannot pick.

    More models to announce as retired than the platform ever recorded. Gated
    like the provider model listing, and rate-limited per admin: every provider
    is asked, an outbound call.
    """
    from app.llm.identity import set_llm_caller

    await enforce_rate_limit(
        request,
        "model_announcement_catalogs",
        limit=CATALOGS_LIMIT,
        window_seconds=CATALOGS_WINDOW_SECONDS,
        bucket=str(user.id),
    )
    # Attribute gateway calls to the requesting admin.
    set_llm_caller(user.id, user.email)
    offered = await announcements.read_offered_models(db)
    catalogs = await announcements.list_provider_catalogs(db)
    return ModelAnnouncementCatalogsResponse(
        models=_models(announcements.unoffered_catalog_models(catalogs, offered)),
        providers=[
            ProviderCatalogSummary(
                provider_id=str(catalog.config_id),
                provider=catalog.provider,
                label=catalog.label,
                model_count=len(catalog.models),
                error=catalog.error,
                truncated=catalog.truncated,
            )
            for catalog in catalogs
        ],
    )


def _retired_choices(
    removed: Sequence[str | RetiredModelChoice],
) -> list[str | announcements.CatalogModel]:
    return [
        announcements.CatalogModel(entry.id, entry.provider) if isinstance(entry, RetiredModelChoice) else entry
        for entry in removed
    ]


async def _chosen_change(
    db: AsyncSession, added: Sequence[str], removed: Sequence[str | RetiredModelChoice]
) -> announcements.CatalogChange:
    try:
        return await announcements.resolve_model_selection(db, added, _retired_choices(removed))
    except announcements.AnnouncementSelectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _test_change(
    db: AsyncSession, body: ModelAnnouncementTestRequest
) -> tuple[announcements.CatalogChange, str, dict[str, object]]:
    """What the test email announces, its name in the audit log and the audit details."""
    from app.llm.router import validate_selectable_chat_model

    if body.added_models is not None or body.removed_models is not None:
        if body.model:
            raise HTTPException(status_code=422, detail="Test either a default model or model changes, not both.")
        change = await _chosen_change(db, body.added_models or [], body.removed_models or [])
        return change, "Model changes", _change_details(change)

    ps = await peek_platform_settings(db)
    current_default = ps.agent_default_model if ps is not None else None
    model = (body.model or "").strip() or None
    if model is None or model == current_default:
        # The current default, as "Email users" would announce it.
        model = current_default
        provider = ps.agent_default_provider if ps is not None else None
        previous = await announcements.previous_default_for(db, model) if model else None
    else:
        # The model about to be saved as the default, replacing the current one.
        if error := await validate_selectable_chat_model(db, model):
            raise HTTPException(status_code=422, detail=error)
        provider = (body.provider or "").strip() or None
        previous = current_default
    if not model:
        raise HTTPException(status_code=422, detail="There is no default model to announce.")
    return (
        announcements.default_model_change(model, provider, previous),
        announcements.model_display_name(model),
        {"default_model": model, "previous_default_model": previous},
    )


@router.post("/preview", response_model=ModelAnnouncementPreviewResponse)
async def preview_model_announcement(
    body: ModelAnnouncementTestRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> ModelAnnouncementPreviewResponse:
    """The email the users would get for ``body``, greeting the calling admin. Nothing is sent."""
    from app.services.branding import get_cached_branding
    from app.services.email import email_greeting_name

    change, _, _ = await _test_change(db, body)
    email = announcements.render_announcement(
        change,
        recipient_name=email_greeting_name(user.full_name),
        platform_name=get_cached_branding().name,
    )
    return ModelAnnouncementPreviewResponse(subject=email.subject, html=email.html, text=email.text)


@router.post("/test", response_model=ModelAnnouncementTestResponse)
async def send_test_model_announcement(
    request: Request,
    body: ModelAnnouncementTestRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
    _ai_gate: None = Depends(require_agentic_ai),
) -> ModelAnnouncementTestResponse:
    """Email the calling admin the announcement the users would get."""
    from app.services.branding import get_cached_branding
    from app.services.email import EmailDeliveryError, deliver_email, email_greeting_name

    try:
        announcements.refuse_unless_emails_allowed()
    except announcements.AnnouncementRefusedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await enforce_rate_limit(
        request,
        "model_announcement_test",
        limit=TEST_EMAIL_LIMIT,
        window_seconds=TEST_EMAIL_WINDOW_SECONDS,
        bucket=str(user.id),
    )

    change, entity_name, details = await _test_change(db, body)
    email = announcements.render_test_announcement(
        change,
        recipient_name=email_greeting_name(user.full_name),
        platform_name=get_cached_branding().name,
    )
    try:
        await deliver_email(to=user.email, subject=email.subject, html=email.html, text=email.text)
    except EmailDeliveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"The test email could not be sent to {user.email}: {exc}",
        ) from exc

    await log_audit(
        db,
        user=user,
        action="trigger",
        entity_type="model_announcement",
        entity_name=entity_name,
        summary=f"Sent a test model announcement to {user.email}",
        details={**details, "test": True},
        request=request,
    )
    await db.commit()
    return ModelAnnouncementTestResponse(sent_to=user.email)


@router.post("", response_model=ModelAnnouncementSummary, status_code=status.HTTP_202_ACCEPTED)
async def announce_default_model(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
    _ai_gate: None = Depends(require_agentic_ai),
) -> ModelAnnouncementSummary:
    """Queue the announcement of the current default model for every user."""
    try:
        row = await announcements.announce_current_default_model(db)
    except announcements.AnnouncementRefusedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await db.flush()
    recipients = await announcements.announcement_recipient_count(db)
    await log_audit(
        db,
        user=user,
        action="trigger",
        entity_type="model_announcement",
        entity_id=row.id,
        entity_name=announcements.model_display_name(row.default_model),
        summary=f"Emailed the users that {announcements.model_display_name(row.default_model)} is the default model",
        details={
            "default_model": row.default_model,
            "previous_default_model": row.previous_default_model,
            "recipients": recipients,
        },
        request=request,
    )
    await db.commit()
    return _summary(
        announcements.AnnouncementProgress(row=row, change=announcements.announcement_change(row), remaining=recipients)
    )


@router.post("/model-changes", response_model=ModelAnnouncementSummary, status_code=status.HTTP_202_ACCEPTED)
async def announce_model_changes(
    body: ModelChangesAnnouncementRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
    _ai_gate: None = Depends(require_agentic_ai),
) -> ModelAnnouncementSummary:
    """Queue, for every user, the email of the models the admin chose as new and as retired."""
    draft_id = (
        parse_path_id(body.draft_id, not_found_detail=CHANGE_NOT_FOUND_DETAIL) if body.draft_id is not None else None
    )
    try:
        row = await announcements.announce_model_changes(
            db, added=body.added_models, removed=_retired_choices(body.removed_models), draft_id=draft_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=CHANGE_NOT_FOUND_DETAIL) from exc
    except announcements.AnnouncementRefusedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except announcements.AnnouncementSelectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.flush()
    change = announcements.announcement_change(row)
    recipients = await announcements.announcement_recipient_count(db)
    await log_audit(
        db,
        user=user,
        action="trigger",
        entity_type="model_announcement",
        entity_id=row.id,
        entity_name="Model changes",
        summary=f"Emailed the users about the model changes ({change.describe()})",
        details={
            **_change_details(change),
            "draft_id": str(draft_id) if draft_id is not None else None,
            "recipients": recipients,
        },
        request=request,
    )
    await db.commit()
    return _summary(announcements.AnnouncementProgress(row=row, change=change, remaining=recipients))
