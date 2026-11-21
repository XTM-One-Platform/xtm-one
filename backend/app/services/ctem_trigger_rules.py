# SPDX-License-Identifier: Apache-2.0

"""CTEM trigger rules (CTEM-08): "a vulnerability on software we run", checked before any model call.

An ``integration_event`` assignment on OpenCTI may declare, in its
``trigger_config``, ``"runs_in_profile": "source"``: a delivery of a
relationship (``has`` from a Software, Infrastructure or System to a
Vulnerability) then starts a run only when its source names a tech stack
product or an asset of the primary organization profile. The receiver reads
the rule after the signature check and before the trigger's filter, gate or
run: a relationship on software the organization does not run costs no model
call and is counted as filtered with its reason.

The signal itself is recorded by the generic OpenCTI listener
(``services/ctem_signal_listeners.py``) with the source in its evidence, so
the scope gate resolves it to the same product. Design:
``dev-docs/ctem-fleets.md`` section "Trigger rules".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The trigger_config key, and the side of the relationship it reads.
RULE_KEY = "runs_in_profile"
RULE_SIDES: tuple[str, ...] = ("source",)


def rule_of(trigger_config: object) -> str:
    """The side a trigger's rule reads, ``""`` when it declares none (an unknown side is no rule)."""
    if not isinstance(trigger_config, Mapping):
        return ""
    side = trigger_config.get(RULE_KEY)
    return side if isinstance(side, str) and side in RULE_SIDES else ""


def _octi_extension(data: Mapping[str, Any]) -> Mapping[str, Any]:
    """The extension that names the relationship's source (OpenCTI's ``extension-definition--...``)."""
    ext = data.get("extensions")
    if not isinstance(ext, Mapping):
        return {}
    return next((v for v in ext.values() if isinstance(v, Mapping) and "source_value" in v), {})


def source_name(payload: object) -> str:
    """The name of the source of the relationship an OpenCTI delivery carries, ``""`` when none.

    A provisioned notifier posts the whole STIX relationship as ``data``: its
    OpenCTI extension names the source (``source_value``, a Software name or
    CPE, an Infrastructure or System name).
    """
    if not isinstance(payload, Mapping):
        return ""
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return ""
    value = _octi_extension(data).get("source_value") or data.get("source_value")
    return value.strip() if isinstance(value, str) else ""


def drop_reason(profile: Any, payload: object) -> str | None:
    """Why the rule drops *payload* for *profile* (``None``: the source runs here, the run starts).

    Pure: *profile* is the primary ``OrganizationProfile`` (``None`` before
    its first save: nothing runs here yet).
    """
    from app.services.ctem_scope_gate import runs_in_profile

    name = source_name(payload)
    if not name:
        return "runs_in_profile: the delivery names no relationship source"
    if profile is None:
        return "runs_in_profile: the organization profile is not saved yet"
    if not runs_in_profile(profile, name):
        return f"runs_in_profile: {name[:120]} is not in the tech stack or the assets"
    return None


async def filter_reason(db: Any, trigger_config: object, payload: object) -> str | None:
    """:func:`drop_reason` for a trigger declaring the rule, the primary profile read; ``None`` otherwise."""
    if not rule_of(trigger_config):
        return None
    from app.services import organization_context as org_context
    from app.services.organizations import get_primary

    primary = await get_primary(db)
    profile = None
    if primary is not None:
        row = await org_context.current_version(db, primary.id)
        profile = org_context.parse_profile(row.profile) if row else None
    return drop_reason(profile, payload)
