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

"""The browser profile session keep-alive: the worker keeps saved sessions signed in.

Run from the periodic hook of ``orchestration_adapters/browser_profiles.py``
on every worker, at most every five minutes per process; a Redis lease keeps
one replica on it at a time, and one sweep refreshes at most one profile.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from app.core.database import async_session
from app.core.utils import format_exception_message
from app.models.browser_profile import SESSION_ACTIVE, SESSION_EXPIRED, SESSION_UNVERIFIED, BrowserProfile

logger = logging.getLogger(__name__)

# Shared by every replica: renamed, an older replica's sweep would run beside a newer one's.
_KEEPALIVE_LEASE = "video_studio:keepalive"
#: The saved sessions the keep-alive probes: signed in, not checked yet, or ended and maybe renewable.
KEEPALIVE_STATUSES: tuple[str, ...] = (SESSION_ACTIVE, SESSION_UNVERIFIED, SESSION_EXPIRED)
#: How often one worker process runs a sweep.
SWEEP_INTERVAL_SECONDS = 300.0


def _now() -> datetime:
    return datetime.now(UTC)


async def keepalive_sweep() -> None:
    """Probe and refresh at most one due profile session.

    The probe opens the start URL in the saved session: signed in, the
    refreshed session is saved (and probed again); not, a profile that can
    sign in by itself is signed in again, and any other one reads ``expired``
    with the URL it landed on.
    """
    from app.services.browser_profiles import signin
    from app.services.browser_profiles.browser import profile_browser
    from app.services.browser_profiles.profiles import record_probe, resolved_snapshot, save_storage_state

    try:
        from app.core.redis import redis_client

        client = await redis_client()
        if not await client.set(_KEEPALIVE_LEASE, "1", nx=True, ex=600):
            return
    except Exception:
        return
    try:
        async with async_session() as db:
            rows = (
                (
                    await db.execute(
                        select(BrowserProfile)
                        .where(
                            BrowserProfile.keepalive_minutes > 0,
                            BrowserProfile.session_status.in_(KEEPALIVE_STATUSES),
                            BrowserProfile.storage_state_enc.isnot(None),
                            or_(
                                BrowserProfile.last_probe_at.is_(None),
                                BrowserProfile.last_probe_at < _now() - timedelta(minutes=30),
                            ),
                        )
                        .order_by(BrowserProfile.last_probe_at.asc().nullsfirst())
                        .limit(5)
                    )
                )
                .scalars()
                .all()
            )
            due = [
                p
                for p in rows
                if p.last_probe_at is None
                or (
                    p.last_probe_at.replace(tzinfo=p.last_probe_at.tzinfo or UTC)
                    < _now() - timedelta(minutes=p.keepalive_minutes)
                )
            ]
            if not due:
                return
            snap = await resolved_snapshot(due[0])
        message = None
        state = None
        learnt: bool | None = None  # whether a renewal signed in without a person
        drove = False
        async with profile_browser(
            policy=snap.policy,
            viewport=(1440, 900),
            pixel_ratio=1,
            storage_state=snap.storage_state,
            color_scheme=snap.options.get("color_scheme"),
            locale=snap.options.get("locale") or "en-US",
            timezone=snap.options.get("timezone") or "UTC",
        ) as rb:
            probe = await signin.probe_in(rb, snap)
            signed_in = probe.authenticated
            if not signed_in and snap.renewable and probe.http_status is None:
                try:
                    renewed = await signin.drive_sign_in(rb, snap, user_id=None, ask=None, check_first=False)
                    signed_in = renewed.ok
                    drove = renewed.ok and renewed.driven
                    if not renewed.ok:
                        message = f"The keep-alive could not sign in again: {renewed.message}"
                except signin.SignInRequiredError as exc:
                    learnt = False
                    message = f"The keep-alive could not sign in again: {exc}"
                except signin.SignInTimeoutError as exc:
                    message = f"The keep-alive could not sign in again: {exc}"
            elif not signed_in and snap.credential_problem and probe.http_status is None:
                message = f"The keep-alive could not sign in again: {snap.credential_problem}."
            if signed_in:
                state, probe = await signin.save_session(rb, snap)
                if drove and probe.authenticated:
                    learnt = True
        async with async_session() as db:
            profile = await db.get(BrowserProfile, uuid.UUID(snap.id))
            if profile is None:
                return
            if learnt is not None and snap.can_sign_in:
                profile.signs_in_unattended = learnt
            if state and (probe.authenticated or probe.http_status is not None):
                # A refreshed session whose re-probe the application could not answer is kept, unverified.
                save_storage_state(profile, state, probe=probe.to_dict())
            else:
                record_probe(profile, probe.to_dict())
                profile.last_sign_in_message = (
                    message
                    or (
                        f"The keep-alive could not check the session: start_url lands on {probe.url or 'nothing'} "
                        f"({probe.reason})."
                        if probe.http_status is not None
                        else f"Session expired: start_url lands on {probe.url or 'nothing'} ({probe.reason})."
                    )
                )[:1000]
            await db.commit()
    except Exception as exc:
        logger.info("Browser profile keep-alive skipped: %s", format_exception_message(exc))
    finally:
        try:
            from app.core.redis import redis_client

            await (await redis_client()).delete(_KEEPALIVE_LEASE)
        except Exception as exc:  # the lease expires by itself
            logger.debug("Keep-alive lease not released: %s", format_exception_message(exc))
