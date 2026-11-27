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

"""The signed-in session an agent's ``browser`` tool opens with a browser profile.

:func:`open_profile_session` launches the profile browser on the profile's
saved session, under the profile's URL policy, and makes sure it opens the
application: the probe of every other use (``signin.ensure_signed_in``), with
a sign-in again when the profile can do it alone (credentials, a TOTP
secret, a saved identity provider session). A profile waiting for a person
(an MFA code, a push approval) is refused with what to ask of them: the agent
loop has no channel for the prompt. Every opening is audited (action
``use``, entity ``browser_profile``) as the person the run acts for.

The session's release (the tool's ``close``, the idle sweep, opening another
profile) writes the refreshed session back - cookies rotate while the agent
browses - only when:

* a probe of it, in a fresh context, still renders the application (an agent
  that signed out never erases the saved session), and
* nobody saved a newer session meanwhile (:func:`store_refreshed_session`:
  compare-and-set on ``session_saved_at`` under a row lock), so a person
  signing in again, the keep-alive or another run is never overwritten.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.core.database import async_session
from app.core.utils import format_exception_message
from app.models.browser_profile import BrowserProfile
from app.services.browser_profiles.agent_access import VIA_GENERAL_ASSISTANT, ProfileAccess
from app.services.browser_profiles.profiles import ProfileSnapshot, resolved_snapshot, save_storage_state

logger = logging.getLogger(__name__)

#: The agent's viewport: the generic browser tool's.
AGENT_VIEWPORT = (1280, 720)
#: Where a person signs a profile in again.
SIGN_IN_HINT = "Ask the person to sign it in on the Browser profiles page (Tools & Integrations), then try again."
#: For a session the application could not answer about (a 5xx, a 429): nothing to sign in again.
UNAVAILABLE_HINT = "The application did not answer: try again later; the saved session was kept."
#: A credential read from a vault could not be read: signing in by hand would hit the same wall.
VAULT_HINT = "Ask an administrator to check the secret vault the profile's credentials are read from (Settings > Access and security > Secret vaults)."


class ProfileSessionError(RuntimeError):
    """A profile session that could not open; ``str()`` says why and what to ask of the person, safe to show."""


@dataclass
class SessionMark:
    """The ``session_saved_at`` an open session's write-back compares against.

    A ``save_session`` from that same session moves it to the save's instant,
    so the refreshed session written back on release is not refused as older.
    """

    saved_at: datetime | None = None


@dataclass(frozen=True)
class ProfileSessionInfo:
    """What the ``browser`` tool knows of the profile its session is signed in with."""

    profile_id: str
    name: str
    start_url: str
    read_only: bool
    #: The write-back's compare-and-set mark; ``None`` for a session with no write-back.
    mark: SessionMark | None = field(default=None, compare=False)


def _same_instant(a: datetime | None, b: datetime | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return a.replace(tzinfo=a.tzinfo or UTC) == b.replace(tzinfo=b.tzinfo or UTC)


async def store_refreshed_session(
    profile_id: str, state: dict, probe: Any, *, saved_at: datetime | None
) -> bool:
    """Save *state* as the profile's session when it still holds the one saved at *saved_at*.

    ``False`` when the profile is gone or a newer session was saved since:
    the newer one wins.
    """
    async with async_session() as db:
        profile = (
            await db.execute(
                select(BrowserProfile).where(BrowserProfile.id == uuid.UUID(profile_id)).with_for_update()
            )
        ).scalar_one_or_none()
        if profile is None or not _same_instant(profile.session_saved_at, saved_at):
            return False
        save_storage_state(profile, state, probe=probe.to_dict())
        await db.commit()
        return True


def profile_hosts(profile: BrowserProfile) -> frozenset[str]:
    """The hosts a profile stands for: its start URL's, its sign-in URL's, its applications' and the ones it declares."""
    from app.services.browser_profiles.browser import normalise_hosts
    from app.services.browser_profiles.profiles import app_hosts

    hosts = set(normalise_hosts(list(profile.allowed_hosts or [])))
    hosts.update(app_hosts(profile.apps))
    hosts.update(normalise_hosts([url for url in (profile.start_url, profile.login_url) if url]))
    return frozenset(hosts)


def _host_in(host: str, hosts: frozenset[str]) -> bool:
    """Whether a cookie or origin *host* belongs to *hosts*: one of them, or a parent domain of one."""
    host = host.lstrip(".").lower().rstrip(".")
    return bool(host) and any(h == host or h.endswith("." + host) for h in hosts)


def scoped_state(captured: Any, hosts: frozenset[str], start_url: str) -> dict:
    """What a profile keeps of a captured session: the cookies and origins of its *hosts* only.

    Whatever else the agent browsed in that session (another site, another
    login) stays out. The page's ``sessionStorage`` is kept when the page is on
    the start URL's origin, as a sign-in keeps it.
    """
    from urllib.parse import urlparse

    from app.services.browser_profiles.browser import SESSION_STORAGE_KEY, origin_of

    state = captured.state or {}
    cookies = [c for c in state.get("cookies") or [] if isinstance(c, dict) and _host_in(str(c.get("domain") or ""), hosts)]
    origins = [
        o
        for o in state.get("origins") or []
        if isinstance(o, dict) and _host_in(urlparse(str(o.get("origin") or "")).hostname or "", hosts)
    ]
    kept: dict = {"cookies": cookies, "origins": origins}
    start = origin_of(start_url)
    if captured.session_storage and start and origin_of(captured.url) == start:
        kept[SESSION_STORAGE_KEY] = {start: dict(captured.session_storage)}
    return kept


async def _store_saved_session(profile_id: str, state: dict, probe: Any, message: str) -> datetime | None:
    """Keep *state* as the profile's session, whatever was saved before: a person chose to save it.

    Returns the new ``session_saved_at``; ``None`` when the profile is gone.
    """
    async with async_session() as db:
        profile = (
            await db.execute(
                select(BrowserProfile).where(BrowserProfile.id == uuid.UUID(profile_id)).with_for_update()
            )
        ).scalar_one_or_none()
        if profile is None:
            return None
        save_storage_state(profile, state, probe=probe.to_dict(), message=message)
        saved_at = profile.session_saved_at
        await db.commit()
        return saved_at


async def _audit_save(access: ProfileAccess, profile: BrowserProfile, *, saved: bool, reason: str) -> None:
    from app.models.user import User
    from app.services.audit import log_audit

    try:
        async with async_session() as db:
            user = await db.get(User, uuid.UUID(str(access.user_id)))
            if user is None:
                return
            await log_audit(
                db,
                user=user,
                action="update",
                entity_type="browser_profile",
                entity_id=str(profile.id),
                entity_name=profile.name,
                summary=(
                    f"Saved an agent's browser session into browser profile {profile.name}"
                    if saved
                    else f"An agent's browser session was not saved into browser profile {profile.name}"
                ),
                details={"via": access.via, "agent_id": access.agent_id, "saved": saved, "reason": reason[:500]},
            )
            await db.commit()
    except Exception as exc:
        logger.warning("Browser profile session save not audited: %s", format_exception_message(exc))


async def _may_manage(access: ProfileAccess, profile: BrowserProfile) -> bool:
    from app.api.v1.helpers import can_manage, get_user_group_ids
    from app.models.user import User

    if not access.user_id:
        return False
    async with async_session() as db:
        user = await db.get(User, uuid.UUID(str(access.user_id)))
        if user is None:
            return False
        return can_manage(user, profile, await get_user_group_ids(db, user.id))


async def save_browser_session(profile: BrowserProfile, captured: Any, *, access: ProfileAccess) -> str:
    """Save the session the agent's browser holds (*captured*) as *profile*'s session.

    The caller resolved *profile* through ``agent_access.resolve_profile``
    (bound, usable by the run's person). Refused, with :class:`ProfileSessionError`,
    when the binding is read-only, the person may not manage the profile, the
    session is signed in with another profile or its page is not on one of the
    profile's hosts. Only the cookies and origins of the profile's hosts are
    kept; the probe of every sign-in (the start URL opened in a fresh context
    holding them, under the profile's URL policy) decides: saved and ``active``
    when it renders the application, nothing saved otherwise. Returns what
    happened, safe to show.
    """
    from urllib.parse import urlparse

    from app.services.browser_profiles import signin

    if captured is None:
        raise ProfileSessionError(
            "No browser session is open in this conversation: sign the application in with the browser first, "
            "then save the session."
        )
    if access.is_read_only(profile.id):
        raise ProfileSessionError(
            f"This agent opens the browser profile '{profile.name}' read-only: it cannot save a session into it."
        )
    current = captured.profile
    if current is not None and str(current.profile_id) != str(profile.id):
        raise ProfileSessionError(
            f"The browser is signed in with the browser profile '{current.name}', not '{profile.name}': its session "
            "is never saved into another profile."
        )
    if not await _may_manage(access, profile):
        raise ProfileSessionError(
            f"The person this run acts for may use the browser profile '{profile.name}' but not edit it: only its "
            "owner or someone it is shared with for configuration can save a session into it."
        )
    hosts = profile_hosts(profile)
    page_host = (urlparse(captured.url).hostname or "").lower().rstrip(".")
    if page_host not in hosts:
        raise ProfileSessionError(
            f"The browser is on {captured.url or 'no page'}, which is not a host of the browser profile "
            f"'{profile.name}' ({', '.join(sorted(hosts))}): navigate to the signed-in application first."
        )
    state = scoped_state(captured, hosts, profile.start_url)
    if not state["cookies"] and not state["origins"]:
        raise ProfileSessionError(
            f"The browser holds no cookie or storage of the hosts of the browser profile '{profile.name}': nothing to "
            "save."
        )
    snap = dataclasses.replace(await resolved_snapshot(profile), storage_state=state)
    probe = await signin.probe_profile(snap)
    if probe.http_status is not None:
        await _audit_save(access, profile, saved=False, reason=probe.reason)
        raise ProfileSessionError(
            f"The session could not be checked: {profile.start_url} did not answer ({probe.reason}). Nothing was saved; "
            "try again later."
        )
    if not probe.authenticated:
        await _audit_save(access, profile, saved=False, reason=probe.reason)
        raise ProfileSessionError(
            f"The session does not open the application: {profile.start_url} landed on {probe.url or 'nothing'} "
            f"({probe.reason}). Nothing was saved: finish the sign-in in the browser, then save again."
        )
    who = "their General Assistant" if access.via == VIA_GENERAL_ASSISTANT else f"agent {access.agent_name or ''}".strip()
    saved_at = await _store_saved_session(str(profile.id), state, probe, f"Session saved from the browser of {who}")
    if saved_at is None:
        raise ProfileSessionError(f"The browser profile '{profile.name}' no longer exists.")
    if current is not None and current.mark is not None:
        # The session opened from this profile writes back on release: it now compares against this save.
        current.mark.saved_at = saved_at
    await _audit_save(access, profile, saved=True, reason=probe.reason)
    return (
        f"Saved the browser's session into the browser profile '{profile.name}' ({len(state['cookies'])} cookie(s), "
        f"{len(state['origins'])} origin(s) of its hosts; nothing of other sites). Checked: {profile.start_url} "
        f"renders the application ({probe.reason}). The profile is signed in."
    )


async def _audit_use(access: ProfileAccess, profile: BrowserProfile, *, read_only: bool, renewed: bool) -> None:
    from app.models.user import User
    from app.services.audit import log_audit

    who = "their General Assistant" if access.via == VIA_GENERAL_ASSISTANT else f"agent {access.agent_name or ''}"
    try:
        async with async_session() as db:
            user = await db.get(User, uuid.UUID(str(access.user_id)))
            if user is None:
                return
            await log_audit(
                db,
                user=user,
                action="use",
                entity_type="browser_profile",
                entity_id=str(profile.id),
                entity_name=profile.name,
                summary=f"Browsed signed in with browser profile {profile.name} through {who.strip()}",
                details={
                    "via": access.via,
                    "agent_id": access.agent_id,
                    "read_only": read_only,
                    "signed_in_again": renewed,
                },
            )
            await db.commit()
    except Exception as exc:
        logger.warning("Browser profile use not audited: %s", format_exception_message(exc))


async def _write_back(rb: Any, snap: ProfileSnapshot, saved_at: datetime | None) -> None:
    from app.services.browser_profiles import signin

    state, probe = await signin.save_session(rb, snap)
    if probe.http_status is not None:
        # The refreshed session is kept unverified: the application could not answer, nothing says it ended.
        logger.info("Agent session of browser profile %s saved unchecked: %s", snap.id, probe.reason)
    elif not probe.authenticated:
        logger.info(
            "Agent session of browser profile %s not saved: it no longer opens the application (%s)",
            snap.id,
            probe.reason,
        )
        return
    if not await store_refreshed_session(snap.id, state, probe, saved_at=saved_at):
        logger.info("Agent session of browser profile %s not saved: a newer session was saved meanwhile", snap.id)


async def open_profile_session(profile: BrowserProfile, *, access: ProfileAccess) -> Any:
    """A ``browser_tool.BrowserSession`` signed in with *profile*, which *access* lets the run open.

    The caller resolved *profile* through ``agent_access.resolve_profile``.
    Raises :class:`ProfileSessionError` when it cannot open signed in.
    """
    from app.services.browser_profiles import signin
    from app.services.browser_profiles.browser import check_target, profile_browser
    from app.services.browser_tool import BrowserSession

    snap = await resolved_snapshot(profile)
    mark = SessionMark(profile.session_saved_at)
    read_only = access.is_read_only(profile.id)
    problem = f" ({snap.credential_problem})" if snap.credential_problem else ""
    sign_in_hint = VAULT_HINT if snap.credential_problem else SIGN_IN_HINT
    if snap.needs_sign_in and not snap.storage_state and not snap.renewable:
        raise ProfileSessionError(
            f"The browser profile '{profile.name}' holds no saved session and cannot sign in by itself{problem}. {sign_in_hint}"
        )
    stack = contextlib.AsyncExitStack()
    try:
        rb = await stack.enter_async_context(
            profile_browser(
                policy=snap.policy,
                viewport=AGENT_VIEWPORT,
                pixel_ratio=1,
                storage_state=snap.storage_state,
                color_scheme=snap.options.get("color_scheme"),
                locale=snap.options.get("locale") or "en-US",
                timezone=snap.options.get("timezone") or "UTC",
            )
        )
        renewed = False
        if snap.needs_sign_in:
            try:
                renewed = await signin.ensure_signed_in(rb, snap) is not None
            except signin.SessionNotSignedInError as exc:
                down = exc.probe.http_status is not None
                if snap.storage_state:
                    await signin.store_probe(
                        snap.id,
                        exc.probe,
                        message=(
                            f"An agent could not check the session: {exc}"
                            if down
                            else f"An agent found the session signed out: {exc}"
                        ),
                    )
                state = "could not be checked" if down else "is not signed in"
                hint = UNAVAILABLE_HINT if down else sign_in_hint
                raise ProfileSessionError(f"The browser profile '{profile.name}' {state} ({exc}{problem}). {hint}") from None
    except BaseException:
        await stack.aclose()
        raise

    async def release() -> None:
        try:
            await asyncio.wait_for(
                _write_back(rb, snap, mark.saved_at), snap.sign_in_timeout + snap.session_save_timeout
            )
        except Exception as exc:
            logger.info("Agent session of browser profile %s not saved: %s", snap.id, format_exception_message(exc))
        finally:
            await stack.aclose()

    await _audit_use(access, profile, read_only=read_only, renewed=renewed)
    return BrowserSession(
        pw=rb.pw,
        browser=rb.browser,
        context=rb.context,
        page=rb.page,
        redirects=rb.redirects,
        check_url=functools.partial(check_target, policy=snap.policy),
        profile=ProfileSessionInfo(
            profile_id=str(profile.id), name=profile.name, start_url=profile.start_url, read_only=read_only, mark=mark
        ),
        release=release,
    )
