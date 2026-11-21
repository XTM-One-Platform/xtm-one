# SPDX-License-Identifier: Apache-2.0

"""The version XTM One registered for each connected module, the one source of a build baseline.

A module (OpenCTI, OpenAEV, OpenCRQ) sends its version on every registration,
which is also its heartbeat; XTM One stores it on ``PlatformRegistration.platform_version``
and the CTEM Command Center chips show that string (``7.261119.0-dev.d736806``).
A module's own version read (GraphQL ``about``, ``/api/settings/version``, tRPC
``health``) can differ from it: without the ``-dev.<sha>`` suffix, or behind or
ahead of the last heartbeat. An agent that judges whether a build carries a fix
reads the registered string; the live answer is a fallback, and a disagreement
between the two is said with both values, never read as an upgrade or a downgrade
(xtm-one #888).

* :func:`registered_modules`: every registration as ``get_platform_overview`` answers it.
* :func:`registered_version_for_integration` and :func:`registered_version_reader`: the
  registration that owns an integration, read when a module's version tool runs.
* :func:`version_answer`: a module version tool's answer with the registered version beside
  the live one.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.models.platform_registration import PlatformRegistration

logger = logging.getLogger(__name__)

#: The commit a development build names after ``-dev.`` (``0.261121.0-dev.dd18c51``).
_DEV_COMMIT = re.compile(r"-dev\.([0-9a-f]{7,40})$", re.IGNORECASE)
#: Longest version shown: the column holds 50 characters.
_MAX_SHOWN = 80


def build_commit(version: str | None) -> str | None:
    """The commit a version string names after ``-dev.``, ``None`` for a release or no version."""
    if not isinstance(version, str):
        return None
    match = _DEV_COMMIT.search(version.strip())
    return match.group(1).lower() if match else None


def _clean(version: object) -> str:
    return version.strip()[:_MAX_SHOWN] if isinstance(version, str) else ""


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


@dataclass(frozen=True)
class RegisteredVersion:
    """What a module's registration says of the build it runs."""

    platform_identifier: str
    version: str
    status: str
    registered_at: datetime | None
    last_heartbeat_at: datetime | None

    @property
    def commit(self) -> str | None:
        return build_commit(self.version)


def _row_version(row: PlatformRegistration) -> RegisteredVersion:
    return RegisteredVersion(
        platform_identifier=row.platform_identifier,
        version=_clean(row.platform_version),
        status=row.status,
        registered_at=row.registered_at,
        last_heartbeat_at=row.last_heartbeat_at or row.registered_at,
    )


async def registered_modules(db: AsyncSession) -> list[dict[str, object]]:
    """Every module registration, the rows the Command Center chips read, oldest first.

    ``version`` is the string the chips show, ``commit`` the one it names after
    ``-dev.`` (``None`` for a release build).
    """
    rows = (await db.execute(select(PlatformRegistration).order_by(PlatformRegistration.created_at))).scalars().all()
    modules: list[dict[str, object]] = []
    for row in rows:
        registered = _row_version(row)
        modules.append(
            {
                # OpenAEV registered under its former identifier is one module (as the chips read it).
                "module": "openaev" if row.platform_identifier == "openbas" else row.platform_identifier,
                "title": row.platform_title,
                "version": registered.version,
                "commit": registered.commit,
                "status": row.status,
                "healthy": row.is_healthy(),
                "registered_at": _iso(registered.registered_at),
                "last_heartbeat_at": _iso(registered.last_heartbeat_at),
            }
        )
    return modules


async def registered_version_for_integration(
    db: AsyncSession, integration_id: uuid.UUID | str
) -> RegisteredVersion | None:
    """The registration that owns *integration_id*, active and most recently seen first; ``None`` when none does."""
    try:
        wanted = integration_id if isinstance(integration_id, uuid.UUID) else uuid.UUID(str(integration_id))
    except ValueError:
        return None
    row = (
        await db.execute(
            select(PlatformRegistration)
            .where(PlatformRegistration.integration_id == wanted)
            .order_by(
                case((PlatformRegistration.status == "active", 0), else_=1),
                PlatformRegistration.last_heartbeat_at.desc().nulls_last(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return _row_version(row) if row is not None else None


#: What a module provider is handed to read its registered version when its version tool runs.
RegisteredVersionFn = Callable[[], Awaitable[RegisteredVersion | None]]


def registered_version_reader(integration_id: str) -> RegisteredVersionFn:
    """A reader of the registration owning *integration_id*, on its own session, at call time.

    Read when the tool runs, not when it is built: a run outlives several heartbeats.
    A registration that cannot be read answers ``None``: the tool then says it has
    the live answer only.
    """

    async def _read() -> RegisteredVersion | None:
        from app.core.database import async_session

        try:
            async with async_session() as db:
                return await registered_version_for_integration(db, integration_id)
        except Exception as exc:
            logger.warning(
                "Could not read the registration of integration %s for its registered version: %s",
                integration_id,
                format_exception_message(exc),
            )
            return None

    return _read


def _same(registered: str, live: str) -> bool:
    return registered.strip().lstrip("vV") == live.strip().lstrip("vV")


def version_answer(
    module: str,
    live_answer: str,
    live_version: str,
    registered: RegisteredVersion | None,
    *,
    quote_live: bool = True,
) -> str:
    """*live_answer* (the module's own version read) with the version XTM One registered beside it.

    The registered version is the source of a build baseline; the live read is a
    fallback, labelled so. When both exist and differ, both are named and no
    direction is inferred from the live read. ``quote_live=False`` names the live
    value as "the live answer above" for a provider that quarantines it.
    """
    if registered is None or not registered.version:
        return (
            f"{live_answer}\n"
            f"Source: {module}'s own live answer. XTM One holds no registered version for this integration, "
            "so this live answer is the only source (a fallback, not the Command Center value)."
        )
    commit = f", commit {registered.commit}" if registered.commit else ""
    lines = [
        live_answer,
        f"Registered in XTM One (the version the CTEM Command Center shows; read this one for a build baseline): "
        f"{json.dumps(registered.version)}{commit}, registration {registered.status}, "
        f"last heartbeat {_iso(registered.last_heartbeat_at) or 'unknown'}.",
    ]
    live = _clean(live_version)
    if not live:
        lines.append(f"{module} gave no live version: the registered version is the only source.")
    elif _same(registered.version, live):
        lines.append("The live answer above (a fallback) agrees with the registered version.")
    else:
        lines.append(
            f"The two sources disagree: registered {json.dumps(registered.version)}, "
            f"{module}'s live answer {json.dumps(live) if quote_live else 'above'} (a fallback). "
            "Judge which build runs on the registered "
            "version; the live answer alone tells no upgrade or downgrade."
        )
    return "\n".join(lines)
