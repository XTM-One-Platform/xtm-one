# SPDX-License-Identifier: Apache-2.0

"""The Filigran license this platform holds, and the Enterprise Edition it grants.

:func:`resolve_xtm_license` reads the license (environment first, then
``PlatformSettings``) against the XTM CA; :func:`ee_platform_status` says
whether a connected platform is Enterprise Edition, from the license's
sub-licenses or the platform's own validated license; and
:func:`xtm_sublicensed_ids` is the platform-type to sub-licensed-ids mapping
both of them and the platform API's registration paths share. The platform
API (edition resolution, ``/config``, registration) and telemetry read them
here, so the two can never report different editions.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.enterprise.licensing import XTMLicenseInfo, get_xtm_license_info
from app.enterprise.xtm_ca import XTM_CA_PEM
from app.models.platform_registration import PlatformRegistration
from app.models.platform_settings import PlatformSettings


def resolve_xtm_license(ps: PlatformSettings | None) -> XTMLicenseInfo:
    """Resolve the Filigran license from env var or DB (PlatformSettings).

    Filigran licenses use the same ``enterprise_license`` field but are
    validated against the XTM CA instead of the Copilot CA.
    """
    db_pem = ps.enterprise_license if ps else None
    return get_xtm_license_info(
        pem_from_env=settings.enterprise_license,
        pem_from_db=db_pem,
        ca_pem=XTM_CA_PEM,
        platform_created_at=ps.created_at if ps else None,
    )


async def ee_platform_status(
    db: AsyncSession,
    xtm_info: XTMLicenseInfo | None = None,
) -> tuple[bool, int]:
    """Return ``(has_ee, active_registration_count)`` for connected platforms.

    A single pass over ``PlatformRegistration`` rows so the frontend can
    distinguish three cases without re-querying:

    * count == 0          → nothing registered yet (welcome landing)
    * count > 0, has_ee   → at least one EE platform (Enterprise Edition)
    * count > 0, !has_ee  → only CE platforms (Community Edition — AI off)

    EE is granted to a registration when EITHER:
      - the Filigran license sub-licenses this platform_id (or ``"global"`` for
        the platform type), OR
      - the platform self-reports a validated EE license (legacy).
    """
    result = await db.execute(
        select(PlatformRegistration).where(
            PlatformRegistration.status == "active",
        )
    )
    active_regs = result.scalars().all()

    if not active_regs:
        return (False, 0)

    has_ee = False

    # Filigran license can grant EE to connected platforms regardless of what
    # the platform itself declares (CE is legacy — the Filigran license is the
    # source of truth for edition).
    if xtm_info and xtm_info.license_validated:
        for reg in active_regs:
            ids = xtm_sublicensed_ids(xtm_info, reg.platform_identifier)
            if not ids:
                continue
            # "global" → any platform of this type qualifies
            if "global" in ids or (reg.platform_id and reg.platform_id in ids):
                has_ee = True
                break

    # Legacy: platform self-reports EE (only need to check if not already true)
    if not has_ee:
        has_ee = any(r.license_validated for r in active_regs)

    return (has_ee, len(active_regs))


# Every platform type a Filigran license sub-licenses (``openbas`` and
# ``opengrc`` are the legacy identifiers of OpenAEV and OpenCRQ).
_MODULE_PLATFORM_TYPES = frozenset({"opencti", "openaev", "openbas", "opencrq", "opengrc"})


def xtm_sublicensed_ids(
    xtm_info: XTMLicenseInfo, platform_identifier: str
) -> list[str]:
    """Sub-licensed platform ids of the Filigran license for a platform type.

    Single source of truth for the platform-type → ids mapping (including
    the legacy ``openbas`` alias for OpenAEV) shared by the platform API's
    ``_registration_ee_enabled`` and :func:`ee_platform_status`, so the two
    EE-resolution paths can never drift.

    A model 2 licence (OID ``62944.120``, issued from 17 Nov 2026) grants
    Enterprise Edition to every module by design: ``["global"]`` for every
    known platform type whatever its module extensions say. A licence without
    the model claim keeps the per-module grants it was issued with.
    """
    if getattr(xtm_info, "covers_every_module", False) and platform_identifier in _MODULE_PLATFORM_TYPES:
        return ["global"]
    type_to_ids: dict[str, list[str]] = {
        "opencti": xtm_info.opencti_ids,
        "openaev": xtm_info.openaev_ids,
        "openbas": xtm_info.openaev_ids,
        "opencrq": xtm_info.opencrq_ids,
        # Legacy identifier for OpenCRQ. New registrations are normalised
        # before they reach here, so this only covers a row that predates
        # both the rename migration and that normalisation.
        "opengrc": xtm_info.opencrq_ids,
    }
    return type_to_ids.get(platform_identifier) or []
