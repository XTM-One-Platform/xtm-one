# SPDX-License-Identifier: Apache-2.0

"""The database half of the security options.

``app/core/security_options.py`` holds the registry and the resolver every
call site reads; this module keeps its snapshot in step with
``platform_settings.security_options`` and writes to it:

* :func:`refresh_security_options_cache` - read the stored values on a session
  of its own and publish them. Called at startup (before the web process
  accepts traffic, when the worker starts), by the GET route, and every
  :data:`~app.core.security_options.REFRESH_INTERVAL_SECONDS` by
  :func:`run_security_options_refresh_loop` in every web and worker process -
  which is what makes a change on one replica apply on the others; the same
  loop refreshes the vault providers connected in Settings > Access and security > Secret
  vaults (``services/vault/store.py``). A failed
  read keeps the previous snapshot. The same read refreshes the trace storage
  and payload switches of Settings > Usage and quotas > Observability
  (``app/services/trace_switches.py``), which the tracing pipeline reads the
  same way: from a snapshot, within the same bound.
* :func:`update_security_options` - the audited write behind the PUT route.
  The caller commits, THEN publishes the returned values: publishing first
  would let this process serve a value a rollback then takes back.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import count_trusted_certificates
from app.core.proxy_headers import TRUST_PROXY_HEADERS_RISK, UPGRADING_SECTION
from app.core.security_options import (
    REFRESH_INTERVAL_SECONDS,
    SECURITY_OPTIONS,
    is_environment_locked,
    is_locked,
    is_pinned,
    normalize_stored,
    publish_stored,
    snapshot_ticket,
)
from app.core.utils import format_exception_message
from app.services.trace_switches import publish_trace_switches, read_trace_switches

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.user import User

# The models are imported where they are used: the worker imports this module
# at startup, and nothing here may pull the ORM registry in before it does.

logger = logging.getLogger(__name__)

#: Options that relax a boundary: set from the UI, they are logged at WARNING
#: like their environment variable is at every boot (``main.py``). A list
#: option's message names its entries (``%s``); a switch's is logged as is.
_RELAXATION_WARNINGS: Mapping[str, str] = {
    "oauth_token_dev_trusted_origins": (
        "OAuth token endpoint guard relaxed for %s (Settings > Access and security) under the "
        "strict token URL checks - loopback token URLs accepted for these origins, "
        "development only. Clear the field outside local testing."
    ),
    "a2a_client_dev_trusted_origins": (
        "A2A outbound destination guard relaxed for %s (Settings > Access and security) - "
        "development only, and every other destination is still refused. Clear "
        "the field outside local testing."
    ),
    "web_fetch_trusted_ca_certificates": (
        "Web page fetches trust administrator-supplied certificates (%s; Settings > Access and "
        "security > Trusted certificates for web fetches) on top of the system store. Verification "
        "stays on. Clear the field to trust the system store alone."
    ),
    # Its variable is reported at boot by ``core/proxy_headers.py``.
    "trust_proxy_headers": (
        '"Trust proxy headers" is on (Settings > Access and security). '
        + TRUST_PROXY_HEADERS_RISK
        + " Otherwise set FORWARDED_ALLOW_IPS to the proxy's addresses and restart, check that the "
        "access log's client_ip shows real client addresses rather than the proxy's, then turn the "
        f"field off. See {UPGRADING_SECTION}."
    ),
}

_warned: dict[str, Any] = {}
_last_refresh_failed = False


class SecurityOptionsError(Exception):
    """A write refused: 403 for an option the platform mode or the environment sets, 422 otherwise."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def read_stored_security_options(db: AsyncSession) -> dict[str, Any]:
    """The valid stored values, normalized (``{}`` when nothing is stored)."""
    from app.models.platform_settings import PlatformSettings

    result = await db.execute(
        select(PlatformSettings.security_options)
        .order_by(PlatformSettings.created_at.asc(), PlatformSettings.id.asc())
        .limit(1)
    )
    return normalize_stored(result.scalar_one_or_none())


def publish_security_options(values: Mapping[str, Any], *, ticket: int | None = None) -> bool:
    """Publish normalized stored values in this process (after the commit).

    *ticket*: the one taken before the read that produced *values*
    (``snapshot_ticket``); without one, the values are the newest - a write
    publishing what it just committed. False when a newer publish already
    happened and these older values were dropped.
    """
    if not publish_stored(values, ticket=ticket):
        return False
    for key, message in _RELAXATION_WARNINGS.items():
        value = None if is_locked(key) else values.get(key)
        if value and _warned.get(key) != value:
            if isinstance(value, bool):
                logger.warning(message)
            elif isinstance(value, str):
                # A PEM value: its count, never the certificates themselves.
                logger.warning(message, _display(value))
            else:
                logger.warning(message, ", ".join(value))
        _warned[key] = value
    return True


async def refresh_security_options_cache() -> bool:
    """Re-read the stored values and the trace switches, and publish them in this process.

    Reads on a session of its own, never a caller's: a failed SELECT aborts
    the transaction it runs in. False when the database could not be read;
    the previous snapshots then stay, and the environment / default applies
    to a process that never read one.
    """
    global _last_refresh_failed
    from app.core.database import async_session

    # Taken before the read starts: a write committed after this point
    # publishes with a later ticket, so this read cannot undo it.
    ticket = snapshot_ticket()
    try:
        async with async_session() as db:
            values = await read_stored_security_options(db)
            switches = await read_trace_switches(db)
    except (SQLAlchemyError, OSError) as exc:
        # Logged once per outage, not on every poll.
        if not _last_refresh_failed:
            logger.warning(
                "Could not read the security options and trace switches (%s); keeping "
                "the values this process already had until the database answers again",
                format_exception_message(exc),
            )
        _last_refresh_failed = True
        return False
    if _last_refresh_failed:
        logger.info("Security options read again from the database")
    _last_refresh_failed = False
    publish_security_options(values, ticket=ticket)
    publish_trace_switches(switches, ticket=ticket)
    return True


async def run_security_options_refresh_loop(interval: float = REFRESH_INTERVAL_SECONDS) -> None:
    """Keep this process's snapshot in step with the database, forever.

    Every web and worker process runs it: the admin write refreshes only the
    process that served it. It only reads, so there is nothing to
    coordinate between replicas.
    """
    from app.services.vault.store import refresh_vault_providers

    while True:
        await asyncio.sleep(interval)
        try:
            await refresh_security_options_cache()
        except Exception:
            # A programming error must not end the loop and freeze the snapshot.
            logger.exception("Security options refresh failed; retrying next cycle")
        # The vault providers connected in Settings ride the same loop.
        try:
            await refresh_vault_providers()
        except Exception:
            logger.exception("Vault providers refresh failed; retrying next cycle")


def _display(wire: Any) -> str:
    if wire is None:
        return "the default"
    if isinstance(wire, bool):
        return "on" if wire else "off"
    if isinstance(wire, list):
        return ", ".join(wire) if wire else "an empty list"
    if isinstance(wire, str) and "-----BEGIN CERTIFICATE-----" in wire:
        # The certificates themselves are in the entry's details, not its summary.
        count = count_trusted_certificates(wire)
        return f"{count} certificate" + ("" if count == 1 else "s")
    if wire == "":
        return "empty"
    return str(wire)


async def update_security_options(
    db: AsyncSession,
    changes: Mapping[str, Any],
    *,
    user: User,
    request: Request | None = None,
) -> dict[str, Any]:
    """Apply an administrator's changes; return the stored values to publish.

    ``None`` for a key clears it, back to the default. Refused as a whole,
    before anything is written: an unknown key or an invalid value (422), an
    option the platform mode pins (403, with the reason - a stored value it
    already holds is kept, not cleared), an option its environment variable
    sets (403 - the variable wins, so a stored value would change nothing and
    the field reads locked). Every change is audited. The caller commits,
    then publishes the result.
    """
    unknown = sorted(key for key in changes if key not in SECURITY_OPTIONS)
    if unknown:
        raise SecurityOptionsError(422, f"Unknown security option(s): {', '.join(unknown)}")
    pinned = sorted(key for key in changes if is_pinned(key))
    if pinned:
        raise SecurityOptionsError(
            403,
            " ".join(
                f'"{SECURITY_OPTIONS[key].label}" ({key}) is read-only: {SECURITY_OPTIONS[key].pinned_reason}.'
                for key in pinned
            ),
        )
    locked = sorted(key for key in changes if is_environment_locked(key))
    if locked:
        names = ", ".join(f"{key} ({SECURITY_OPTIONS[key].env_var})" for key in locked)
        raise SecurityOptionsError(
            403,
            f"The following security options are set by environment variables and are "
            f"read-only: {names}. Update the environment variable(s) and restart the "
            "backend, or remove them to manage these options in Settings > Access and security.",
        )
    normalized: dict[str, Any] = {}
    problems: list[str] = []
    for key, value in changes.items():
        if value is None:
            normalized[key] = None
            continue
        try:
            normalized[key] = SECURITY_OPTIONS[key].normalize(value)
        except ValueError as exc:
            problems.append(f"{key}: {exc}")
    if problems:
        raise SecurityOptionsError(422, "; ".join(problems))

    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.models.platform_settings import PlatformSettings
    from app.services.audit import log_audit

    await get_platform_settings(db)  # the singleton must exist to be locked
    # A row lock, and the row as it is AFTER a concurrent writer commits
    # (``populate_existing``): two administrators saving different options at
    # once must both land, not the second one's snapshot of the first's row.
    ps = (
        await db.execute(
            select(PlatformSettings)
            .order_by(PlatformSettings.created_at.asc(), PlatformSettings.id.asc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    raw = dict(ps.security_options) if isinstance(ps.security_options, Mapping) else {}
    current = normalize_stored(raw)

    changed: list[tuple[str, Any, Any]] = []
    for key, value in normalized.items():
        option = SECURITY_OPTIONS[key]
        before = option.to_wire(current[key]) if key in current else None
        if value is None:
            if key in raw:
                raw.pop(key)
                changed.append((key, before, None))
            continue
        after = option.to_wire(value)
        if key not in current or before != after:
            # Keys this release does not know (written by a newer one) stay
            # untouched in ``raw``.
            raw[key] = after
            changed.append((key, before, after))

    if changed:
        ps.security_options = raw or None
        await db.flush()
        for key, before, after in changed:
            option = SECURITY_OPTIONS[key]
            await log_audit(
                db,
                user=user,
                action="configure",
                entity_type="security",
                entity_name="Security options",
                summary=(
                    f"Security option {option.env_var} reset to the default"
                    if after is None
                    else f"Security option {option.env_var} set to {_display(after)}"
                ),
                details={"setting": key, "env_var": option.env_var, "from": before, "to": after},
                request=request,
            )
    return normalize_stored(raw)
