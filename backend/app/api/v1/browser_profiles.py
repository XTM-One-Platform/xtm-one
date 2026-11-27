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

"""Browser profiles API: saved sign-ins every browser of the platform can open.

A profile carries the three-tier visibility set: using one (signing it in,
checking its session, answering its MFA prompt) needs the normal predicate
without the admin bypass, managing it ``can_manage``; credentials are
write-only (``services/browser_profiles/profiles.py``). Going private removes
the profile from other people's agents (``Agent.browser_profile_ids``).

Not behind the Video Studio feature flag: agents browse with profiles too.

A credential may be a vault reference: a write naming one is held to
the caller's grants (``require_vault_writer``), an edit moving where the
profile signs in (start URL, sign-in URL, private hosts) to the re-point rule
of every secret field (``retained_reference_writer_problem``), and responses
say where each credential reads (``vault_secrets``), the path only to a
manager who may use it.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.helpers import (
    can_manage,
    get_user_group_ids,
    handle_company_managed_toggle,
    parse_path_id,
    user_visible_filter,
)
from app.api.v1.schemas.browser_profiles import BrowserProfileList, BrowserProfileWrite, MfaCodeRequest, SignInState
from app.api.v1.schemas.vault_references import attach_vault_failures, require_vault_writer, vault_status
from app.core.database import async_session, get_db
from app.core.utils import format_exception_message
from app.models.browser_profile import KEEPALIVE_MINUTES_DEFAULT, BrowserProfile
from app.models.file_attachment import FileAttachment
from app.models.user import User
from app.services.audit import log_audit
from app.services.browser_profiles.profiles import (
    ProfileError,
    apply_fields,
    clean_fields,
    covers_url,
    destination,
    load_usable_profile,
    referenced_credentials,
    repoint_problem,
    resolved_snapshot,
    save_storage_state,
    serialize_profile,
    write_credentials,
)
from app.services.vault import stored_reference_providers
from app.services.vault.store import vault_principal

router = APIRouter()

_NOT_FOUND = "Not found"
#: A session check answers within this bound: the probe (its retries of an unavailable application included) and
#: the wait for an unattended renewal share it.
CHECK_TIMEOUT_SECONDS = 90.0
#: The Agent column a profile going private is removed from, on other people's agents.
AGENT_FIELD = "browser_profile_ids"


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail=_NOT_FOUND)


async def _visible_profile(db: AsyncSession, profile_id: str, user: User) -> BrowserProfile:
    profile = await db.get(BrowserProfile, parse_path_id(profile_id, not_found_detail=_NOT_FOUND))
    if profile is None:
        raise _not_found()
    groups = await get_user_group_ids(db, user.id)
    visible = (
        await db.execute(
            select(BrowserProfile.id).where(
                BrowserProfile.id == profile.id,
                user_visible_filter(BrowserProfile, user.id, groups, is_admin=user.role == "admin"),
            )
        )
    ).scalar_one_or_none()
    if visible is None:
        raise _not_found()
    return profile


async def _manageable_profile(db: AsyncSession, profile_id: str, user: User) -> BrowserProfile:
    profile = await _visible_profile(db, profile_id, user)
    if not can_manage(user, profile, await get_user_group_ids(db, user.id)):
        raise HTTPException(status_code=403, detail="You can use this profile but not change it.")
    return profile


async def _usable_profile(db: AsyncSession, profile_id: str, user: User) -> BrowserProfile:
    profile = await load_usable_profile(db, parse_path_id(profile_id, not_found_detail=_NOT_FOUND), user.id)
    if profile is None:
        raise _not_found()
    return profile


async def _served(db: AsyncSession, user: User, rows: list[tuple[BrowserProfile, bool]]) -> list[dict]:
    """Each ``(profile, may manage)`` as served, with where its credentials read from a vault and their last failed read."""
    principal = (
        await vault_principal(db, user)
        if any(manage and referenced_credentials(profile) for profile, manage in rows)
        else None
    )
    holders = [
        SimpleNamespace(id=profile.id, vault_secrets=vault_status(profile, principal if manage else None))
        for profile, manage in rows
    ]
    await attach_vault_failures(holders, BrowserProfile.__tablename__)
    served = []
    for (profile, manage), holder in zip(rows, holders, strict=True):
        data = serialize_profile(profile, can_manage=manage)
        data["vault_secrets"] = holder.vault_secrets.model_dump() if holder.vault_secrets is not None else None
        served.append(data)
    return served


async def _served_one(db: AsyncSession, user: User, profile: BrowserProfile, *, manage: bool | None = None) -> dict:
    if manage is None:
        manage = can_manage(user, profile, await get_user_group_ids(db, user.id))
    return (await _served(db, user, [(profile, manage)]))[0]


def _vault_providers(profile: BrowserProfile) -> list[str]:
    """The vault providers the profile's credentials read from, for the audit trail (never a path)."""
    return stored_reference_providers(profile, ("credentials_enc",)).get("credentials", [])


@router.get("")
async def list_profiles(
    url: str | None = None, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> BrowserProfileList:
    """The profiles the caller may use; with *url*, those whose application or declared applications cover its host."""
    groups = await get_user_group_ids(db, user.id)
    rows = (
        (
            await db.execute(
                select(BrowserProfile)
                .where(user_visible_filter(BrowserProfile, user.id, groups, is_admin=False))
                .order_by(BrowserProfile.name)
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    if url:
        rows = [profile for profile in rows if covers_url(profile, url)]
    return BrowserProfileList(items=await _served(db, user, [(p, can_manage(user, p, groups)) for p in rows]))


def _write_profile(profile: BrowserProfile, body: BrowserProfileWrite) -> bool:
    fields = clean_fields(
        name=body.name,
        start_url=body.start_url,
        login_url=body.login_url,
        allowed_hosts=body.allowed_hosts,
        # Omitted: keep the profile's applications.
        apps=[app.model_dump() for app in body.apps] if body.apps is not None else list(profile.apps or []),
        login_config=body.login_config,
        options=body.browser_options,
        description=body.description,
        # Omitted: keep the profile's interval, the default (240 minutes) on a new one.
        keepalive_minutes=body.keepalive_minutes
        if body.keepalive_minutes is not None
        else (profile.keepalive_minutes if profile.keepalive_minutes is not None else KEEPALIVE_MINUTES_DEFAULT),
        timeouts={
            "sign_in_timeout_seconds": body.sign_in_timeout_seconds,
            "session_save_timeout_seconds": body.session_save_timeout_seconds,
            "mfa_timeout_seconds": body.mfa_timeout_seconds,
        },
    )
    apply_fields(profile, fields)
    creds = {
        k: v
        for k, v in (("username", body.username), ("password", body.password), ("totp_secret", body.totp_secret))
        if v is not None
    }
    return write_credentials(profile, **creds) if creds else False


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_profile(
    body: BrowserProfileWrite, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    await require_vault_writer(db, user, username=body.username, password=body.password, totp_secret=body.totp_secret)
    profile = BrowserProfile(user_id=user.id, name=body.name, start_url=body.start_url)
    try:
        changed = _write_profile(profile, body)
    except ProfileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    db.add(profile)
    await db.flush()
    if body.is_company_managed:
        await handle_company_managed_toggle(db, user, profile, True, agent_field=AGENT_FIELD)
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="browser_profile",
        entity_id=str(profile.id),
        entity_name=profile.name,
        summary=f"Created browser profile {profile.name}",
        details={"hosts": profile.allowed_hosts, "credentials_changed": changed, "vault_providers": _vault_providers(profile)},
    )
    await db.commit()
    return await _served_one(db, user, profile, manage=True)


@router.get("/{profile_id}")
async def get_profile(
    profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    profile = await _visible_profile(db, profile_id, user)
    return await _served_one(db, user, profile)


@router.put("/{profile_id}")
async def update_profile(
    profile_id: str,
    body: BrowserProfileWrite,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    profile = await _manageable_profile(db, profile_id, user)
    await require_vault_writer(db, user, username=body.username, password=body.password, totp_secret=body.totp_secret)
    before = destination(profile)
    try:
        changed = _write_profile(profile, body)
    except ProfileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if retained := await repoint_problem(db, user, profile, before):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=retained)
    await handle_company_managed_toggle(db, user, profile, body.is_company_managed, agent_field=AGENT_FIELD)
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="browser_profile",
        entity_id=str(profile.id),
        entity_name=profile.name,
        summary=f"Updated browser profile {profile.name}",
        details={"hosts": profile.allowed_hosts, "credentials_changed": changed, "vault_providers": _vault_providers(profile)},
    )
    await db.commit()
    return await _served_one(db, user, profile, manage=True)


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(
    profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Response:
    profile = await _manageable_profile(db, profile_id, user)
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="browser_profile",
        entity_id=str(profile.id),
        entity_name=profile.name,
        summary=f"Deleted browser profile {profile.name}",
    )
    await db.delete(profile)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{profile_id}/sign-in")
async def sign_in_profile(
    profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> SignInState:
    from app.services.browser_profiles import signin

    profile = await _usable_profile(db, profile_id, user)
    await log_audit(
        db,
        user=user,
        action="trigger",
        entity_type="browser_profile",
        entity_id=str(profile.id),
        entity_name=profile.name,
        summary=f"Signed browser profile {profile.name} in",
    )
    await db.commit()
    outcome = await signin.start_sign_in(profile, user_id=user.id, wait_seconds=45.0)
    return SignInState(**outcome.to_dict())


@router.post("/{profile_id}/test-sign-in")
async def test_sign_in_profile(
    profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> SignInState:
    """Run the whole sign-in once as a dry run: save nothing, and return the trace so the operator sees where it stalls."""
    from app.services.browser_profiles import signin

    profile = await _usable_profile(db, profile_id, user)
    await log_audit(
        db,
        user=user,
        action="trigger",
        entity_type="browser_profile",
        entity_id=str(profile.id),
        entity_name=profile.name,
        summary=f"Tested the sign-in of browser profile {profile.name} (dry run)",
    )
    await db.commit()
    outcome = await signin.dry_run_sign_in(profile, user_id=user.id)
    return SignInState(**outcome.to_dict())


@router.post("/{profile_id}/check")
async def check_profile_session(
    profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """Probe the saved session; when it has expired and the profile renews unattended (a valid TOTP secret, or a
    saved identity-provider session), sign in again by itself. Answer the profile with the resulting status."""
    from app.services.browser_profiles import signin

    profile = await _usable_profile(db, profile_id, user)
    await log_audit(
        db,
        user=user,
        action="trigger",
        entity_type="browser_profile",
        entity_id=str(profile.id),
        entity_name=profile.name,
        summary=f"Checked the session of browser profile {profile.name}",
    )
    snap = await resolved_snapshot(profile)
    await db.commit()
    started = time.monotonic()
    try:
        probe = await asyncio.wait_for(signin.probe_profile(snap), CHECK_TIMEOUT_SECONDS)
    except TimeoutError:
        raise HTTPException(status_code=503, detail="The session check did not finish in time: try again.") from None
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"The session check could not run: {format_exception_message(exc)}"
        ) from None
    if probe.authenticated:
        if snap.storage_state:
            await signin.store_probe(snap.id, probe)
    elif snap.renews_unattended and probe.http_status is None:
        # The renewal waits only for what is left of the bound: past it, the profile answers ``signing_in``
        # and the sign-in goes on in the background.
        left = max(0.0, CHECK_TIMEOUT_SECONDS - (time.monotonic() - started))
        await signin.start_sign_in(profile, user_id=user.id, wait_seconds=left)
    elif snap.storage_state:
        problem = snap.credential_problem if probe.http_status is None else None
        await signin.store_probe(
            snap.id, probe, message=f"The session expired and could not be renewed: {problem}." if problem else None
        )
    await db.refresh(profile)
    return await _served_one(db, user, profile)


@router.post("/{profile_id}/sign-out")
async def sign_out_profile(
    profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    profile = await _manageable_profile(db, profile_id, user)
    save_storage_state(profile, None, message="The saved session was forgotten.")
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="browser_profile",
        entity_id=str(profile.id),
        entity_name=profile.name,
        summary=f"Forgot the session of browser profile {profile.name}",
    )
    await db.commit()
    return await _served_one(db, user, profile, manage=True)


@router.get("/{profile_id}/mfa-screenshot")
async def mfa_screenshot(
    profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Response:
    """The screenshot of the profile's open MFA prompt, for whoever may use the profile."""
    from app.services.file_responses import file_response_headers
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    profile = await _usable_profile(db, profile_id, user)
    file_att = await db.get(FileAttachment, profile.mfa_screenshot_file_id) if profile.mfa_screenshot_file_id else None
    if file_att is None:
        raise _not_found()
    try:
        data = await file_storage.download(file_att.s3_object_name)
    except FileNotFoundInStorageError:
        raise _not_found() from None
    return Response(content=data, media_type=file_att.content_type, headers=file_response_headers(file_att, store=False))


@router.post("/mfa/{challenge_id}")
async def submit_mfa(challenge_id: str, body: MfaCodeRequest, user: User = Depends(get_current_user)) -> SignInState:
    from app.services.browser_profiles import signin

    state = await signin.read_state(challenge_id)
    if state is None or not state.profile_id:
        raise _not_found()
    async with async_session() as db:
        if await load_usable_profile(db, state.profile_id, user.id) is None:
            raise _not_found()
    outcome = await signin.submit_code(challenge_id, body.code, wait_seconds=40.0)
    return SignInState(**outcome.to_dict())


@router.get("/mfa/{challenge_id}")
async def get_mfa(challenge_id: str, user: User = Depends(get_current_user)) -> SignInState:
    from app.services.browser_profiles import signin

    state = await signin.read_state(challenge_id)
    if state is None or not state.profile_id:
        raise _not_found()
    async with async_session() as db:
        if await load_usable_profile(db, state.profile_id, user.id) is None:
            raise _not_found()
    return SignInState(**state.to_dict())
