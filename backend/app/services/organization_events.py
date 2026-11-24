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

"""Which tracked organization an OpenCTI event concerns (#9157, slice M4).

Design: ``dev-docs/ctem-fleets.md`` section 9.4. A trigger provisioned with
``provision.organizations`` (``services/opencti_trigger_provisioning.py``)
serves every monitored organization at once; its state keeps, per
organization, the OpenCTI ids (internal and standard) of its identity and
sectors. The receiver matches the ids the delivered STIX instance carries
(``target_ref``, ``where_sighted_refs``, ``object_refs``, ...) against them
and records the organizations in the run's trigger payload
(:data:`PAYLOAD_KEY`); the run block then carries their profiles next to the
primary's (``organization_context.fleet_run_context``).

An identity match wins over a sector match: a report naming the provider
concerns the provider; a sector match concerns every organization of that
sector only when no identity matched. Nothing is guessed: no match, no
organization.
"""

from __future__ import annotations

import uuid
from typing import Any

#: Where the receiver records the concerned organizations' XTM One ids.
PAYLOAD_KEY = "_organizations"
#: Bounds of the walk over a delivered instance (a large report is cut, never slow).
_MAX_DEPTH = 4
_MAX_STRINGS = 5000


def _strings(value: Any, depth: int = 0, out: set[str] | None = None) -> set[str]:
    out = set() if out is None else out
    if len(out) >= _MAX_STRINGS or depth > _MAX_DEPTH:
        return out
    if isinstance(value, str):
        out.add(value)
    elif isinstance(value, dict):
        for item in value.values():
            _strings(item, depth + 1, out)
    elif isinstance(value, list):
        for item in value:
            _strings(item, depth + 1, out)
    return out


def concerned_organizations(data: Any, organizations: Any) -> list[str]:
    """The XTM One ids of the *organizations* (provisioning state entries) *data* names, identity first."""
    if not isinstance(organizations, list) or not organizations:
        return []
    seen = _strings(data)
    if not seen:
        return []
    entries = [o for o in organizations if isinstance(o, dict) and o.get("id")]
    by_identity = [str(o["id"]) for o in entries if seen & set(o.get("identity_ids") or [])]
    if by_identity:
        return sorted(set(by_identity))
    return sorted({str(o["id"]) for o in entries if seen & set(o.get("sector_ids") or [])})


def named_organizations(data: Any, organizations: Any) -> list[str]:
    """The XTM One ids of the tracked *organizations* whose OpenCTI identity *data* names (CTEM-29).

    Identity only: a sector match concerns a tracked organization's sector, not the
    organization, and never makes a third-party event.
    """
    if not isinstance(organizations, list) or not organizations:
        return []
    seen = _strings(data)
    entries = [o for o in organizations if isinstance(o, dict) and o.get("id")]
    return sorted({str(o["id"]) for o in entries if seen & set(o.get("identity_ids") or [])})


def named_in_payload(payload: dict[str, Any], trigger_config: Any) -> list[str]:
    """The tracked organizations an OpenCTI delivery to *trigger_config* names by identity."""
    from app.services.opencti_trigger_provisioning import STATE_KEY

    state = trigger_config.get(STATE_KEY) if isinstance(trigger_config, dict) else None
    if not isinstance(state, dict):
        return []
    return named_organizations(payload.get("data"), state.get("organizations"))


def annotate_payload(payload: dict[str, Any], trigger_config: Any) -> list[str]:
    """Record in *payload* the organizations its OpenCTI event concerns; returns them (in place)."""
    from app.services.opencti_trigger_provisioning import STATE_KEY

    if not isinstance(trigger_config, dict):
        return []
    state = trigger_config.get(STATE_KEY)
    if not isinstance(state, dict):
        return []
    found = concerned_organizations(payload.get("data"), state.get("organizations"))
    if found:
        payload[PAYLOAD_KEY] = found
    return found


def organization_ids_of(trigger_payload: Any) -> list[uuid.UUID]:
    """The organization ids the receiver recorded on a run's trigger payload (invalid ones dropped)."""
    raw = trigger_payload.get(PAYLOAD_KEY) if isinstance(trigger_payload, dict) else None
    ids: list[uuid.UUID] = []
    for value in raw if isinstance(raw, list) else []:
        try:
            ids.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    return ids[:20]
