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

"""Capability slots of a fleet's roles (#914, slice 2 of the customer layer).

A built-in fleet only works on the customer's own estate once each role
reaches the right kind of tool: Mobilization needs a ticketing and a
notification channel, Validation an EDR/XDR, a scanner or an EASM, Triage
vulnerability intelligence and an asset inventory. Each role label declares
the capabilities it can use (:data:`ROLE_SLOTS`); a capability is a set of
catalog ``integration_types`` (:data:`CAPABILITIES`), so any provider of the
catalog typed that way fills it, with no provider named here.

A slot reads, for one member, ``filled`` (a bound integration of the
capability), ``empty`` (none bound, at least one connected that the reader
may bind) or ``unavailable`` (none connected). Binding a connected
integration to a slot adds it to the member agent's ``integration_bindings``:
the customer layer of a built-in (``services.builtin_customer_layer``), which
the seed never rewrites.

Pure functions only: the router loads the rows and writes the agent.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: Capability key -> label and the catalog ``integration_types`` that fill it.
CAPABILITIES: dict[str, dict[str, Any]] = {
    "ticketing": {"label": "Ticketing", "integration_types": ("ticketing", "itsm", "project_management")},
    "notification": {"label": "Notification", "integration_types": ("messaging", "incident_management")},
    "edr_xdr": {"label": "EDR / XDR", "integration_types": ("edr", "xdr")},
    "scanner": {
        "label": "Vulnerability scanner",
        "integration_types": ("vulnerability_management", "application_security", "cloud_security"),
    },
    "easm": {"label": "External attack surface", "integration_types": ("asm",)},
    "vulnerability_intelligence": {"label": "Vulnerability intelligence", "integration_types": ("threat_intel",)},
    "asset_inventory": {"label": "Asset inventory", "integration_types": ("cmdb", "endpoint_management")},
    "siem": {"label": "SIEM", "integration_types": ("siem",)},
}

#: Role label (as the fleet seeds it on a member) -> the capabilities it can use, in order.
ROLE_SLOTS: dict[str, tuple[str, ...]] = {
    "Mobilization": ("ticketing", "notification"),
    "Validation": ("edr_xdr", "scanner", "easm"),
    "Triage": ("vulnerability_intelligence", "asset_inventory"),
    "Relevance": ("vulnerability_intelligence", "asset_inventory"),
    "Hunt": ("siem", "edr_xdr"),
    "Detections": ("siem", "edr_xdr"),
    "Gap analysis": ("siem", "edr_xdr"),
    "Discovery": ("scanner", "easm", "asset_inventory"),
}

SLOT_FILLED = "filled"
SLOT_EMPTY = "empty"
SLOT_UNAVAILABLE = "unavailable"


class SlotBindingRefusedError(Exception):
    """A bind or unbind the slot rules refuse; ``status_code`` and ``detail`` for the API."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def slots_for_role(role_label: str | None) -> tuple[str, ...]:
    """The capabilities a role label declares; none for a label without slots."""
    return ROLE_SLOTS.get((role_label or "").strip(), ())


def providers_for(capability: str, catalog: Mapping[str, Any]) -> frozenset[str]:
    """The catalog providers whose ``integration_types`` fill *capability*."""
    types = set(CAPABILITIES[capability]["integration_types"])
    return frozenset(key for key, entry in catalog.items() if types & set(entry.get("integration_types") or ()))


@dataclass
class IntegrationRef:
    """An integration as a slot shows it."""

    integration_id: str
    provider: str
    name: str
    company_managed: bool = False


@dataclass
class SlotState:
    capability: str
    label: str
    state: str
    bound: list[IntegrationRef] = field(default_factory=list)
    candidates: list[IntegrationRef] = field(default_factory=list)


def bound_ids(bindings: Mapping[str, Any] | None) -> dict[str, set[str]]:
    """``integration_bindings`` as provider -> set of ids (a scalar value read as one id)."""
    out: dict[str, set[str]] = {}
    for provider, ids in (bindings or {}).items():
        values = ids if isinstance(ids, (list, tuple, set)) else [ids]
        out[str(provider)] = {str(v) for v in values if v}
    return out


def slot_states(
    role_label: str | None,
    bindings: Mapping[str, Any] | None,
    connected: Iterable[IntegrationRef],
    catalog: Mapping[str, Any],
    *,
    company_managed_only: bool = False,
) -> list[SlotState]:
    """Each slot of *role_label* for an agent holding *bindings*.

    *connected* are the connected integrations the reader can see. A
    company-managed agent takes company-managed integrations only
    (``agent_bindings.validate_company_managed_bindings``), so its candidates
    are narrowed to them.
    """
    held = bound_ids(bindings)
    pool = [i for i in connected if i.company_managed or not company_managed_only]
    by_id = {i.integration_id: i for i in connected}
    states: list[SlotState] = []
    for capability in slots_for_role(role_label):
        providers = providers_for(capability, catalog)
        bound = [
            by_id.get(iid) or IntegrationRef(integration_id=iid, provider=provider, name="")
            for provider in sorted(providers & held.keys())
            for iid in sorted(held[provider])
        ]
        bound_set = {b.integration_id for b in bound}
        candidates = [i for i in pool if i.provider in providers and i.integration_id not in bound_set]
        if bound:
            state = SLOT_FILLED
        elif candidates:
            state = SLOT_EMPTY
        else:
            state = SLOT_UNAVAILABLE
        states.append(
            SlotState(
                capability=capability,
                label=CAPABILITIES[capability]["label"],
                state=state,
                bound=bound,
                candidates=candidates,
            )
        )
    return states


def _check_slot(role_label: str | None, capability: str) -> None:
    if capability not in CAPABILITIES:
        raise SlotBindingRefusedError(422, f"Unknown capability '{capability}'.")
    if capability not in slots_for_role(role_label):
        role = (role_label or "").strip() or "this member"
        raise SlotBindingRefusedError(422, f"The role '{role}' has no '{capability}' slot.")


def bindings_with(
    role_label: str | None,
    capability: str,
    bindings: Mapping[str, Any] | None,
    integration: IntegrationRef,
    catalog: Mapping[str, Any],
    *,
    company_managed_only: bool = False,
) -> dict[str, list[str]]:
    """*bindings* with *integration* bound for *capability*, refused when it does not fit."""
    _check_slot(role_label, capability)
    if integration.provider not in providers_for(capability, catalog):
        raise SlotBindingRefusedError(
            422,
            f"The integration '{integration.name or integration.integration_id}' does not provide "
            f"the '{CAPABILITIES[capability]['label']}' capability.",
        )
    if company_managed_only and not integration.company_managed:
        raise SlotBindingRefusedError(
            400,
            "Company-managed agents can only use company-managed integrations. "
            f'"{integration.name}" is not company-managed.',
        )
    held = bound_ids(bindings)
    held.setdefault(integration.provider, set()).add(integration.integration_id)
    return {provider: sorted(ids) for provider, ids in held.items() if ids}


def bindings_without(
    role_label: str | None,
    capability: str,
    bindings: Mapping[str, Any] | None,
    integration_id: str,
    catalog: Mapping[str, Any],
) -> dict[str, list[str]]:
    """*bindings* with *integration_id* unbound from *capability*'s providers."""
    _check_slot(role_label, capability)
    providers = providers_for(capability, catalog)
    held = bound_ids(bindings)
    if not any(integration_id in held.get(p, set()) for p in providers):
        raise SlotBindingRefusedError(404, "This integration is not bound to that slot.")
    for provider in providers:
        held.get(provider, set()).discard(integration_id)
    return {provider: sorted(ids) for provider, ids in held.items() if ids}
