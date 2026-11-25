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
vulnerability intelligence and an asset inventory, plus application security
and source code for the code flaws of the customer's own software (#1434).
Each role label declares
the capabilities it can use (:data:`ROLE_SLOTS`); a capability is a set of
catalog ``integration_types`` (:data:`CAPABILITIES`), so any provider of the
catalog typed that way fills it, with no provider named here.

A slot reads, for one member, ``filled`` (a bound integration of the
capability), ``empty`` (none bound, at least one connected that the reader
may bind) or ``unavailable`` (none connected). Binding a connected
integration to a slot adds it to the member agent's ``integration_bindings``:
the customer layer of a built-in (``services.builtin_customer_layer``), which
the seed never rewrites.

Capabilities by type (#1201): a fleet makes one choice per capability
(:func:`type_states`), written on every member slot of that type
(:func:`members_to_write`); a member whose bound set is not empty and differs
from the fleet choice is ``custom`` and is kept by a later type write unless the person
confirms the override. Two capabilities carry a built-in option that needs no
integration and is never stored as a binding (:data:`BUILTIN_OPTIONS`): the
XTM One inventory (the organization context assets) for the asset inventory,
and in-app delivery (bell, decisions, work items) for notification, which is
always on so an external channel only adds to it. Notification also takes the
``email`` type: the built-in Platform email and mail integrations fill it.

A connected integration of the right type is never hidden (#1373): one the
member cannot take (not company-managed for a company-managed agent, or not
connected yet) is listed in ``blocked`` with its reason, and a type with
nothing bound or bindable but something blocked reads ``not_usable``, never
``none_connected``.

Pure functions only: the router loads the rows and writes the agent.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

#: Capability key -> label and the catalog ``integration_types`` that fill it.
CAPABILITIES: dict[str, dict[str, Any]] = {
    "ticketing": {"label": "Ticketing", "integration_types": ("ticketing", "itsm", "project_management")},
    "notification": {"label": "Notification", "integration_types": ("messaging", "incident_management", "email")},
    "edr_xdr": {"label": "EDR / XDR", "integration_types": ("edr", "xdr")},
    # Infrastructure and cloud scanners. A provider also typed application_security
    # (Aikido) belongs to Application security only: ``excluded_types`` (#1434).
    "scanner": {
        "label": "Vulnerability scanner",
        "integration_types": ("vulnerability_management", "cloud_security"),
        "excluded_types": ("application_security",),
    },
    # Code, dependency, secret, container and DAST findings (Aikido): the code_flaw and
    # credential_leak signals of the CTEM loop (#1434).
    "application_security": {"label": "Application security", "integration_types": ("application_security",)},
    # The code hosts that hold the source_code assets (repositories, pipelines, packages)
    # and their dependency alerts: GitHub, GitLab (#1434).
    "source_code": {"label": "Source code", "integration_types": ("repository",)},
    "easm": {"label": "External attack surface", "integration_types": ("asm",)},
    "vulnerability_intelligence": {"label": "Vulnerability intelligence", "integration_types": ("threat_intel",)},
    "asset_inventory": {"label": "Asset inventory", "integration_types": ("cmdb", "endpoint_management")},
    "siem": {"label": "SIEM", "integration_types": ("siem",)},
}

#: Role label (as the fleet seeds it on a member) -> the capabilities it can use, in order.
ROLE_SLOTS: dict[str, tuple[str, ...]] = {
    # Mobilize files a ticket or applies the fix through a bound integration (#2061): an IOC
    # pushed to the EDR in prevention mode, a rule deployed, under ctem.production_change.
    "Mobilization": ("ticketing", "notification", "edr_xdr", "siem"),
    "Validation": ("edr_xdr", "scanner", "easm", "application_security"),
    "Triage": ("vulnerability_intelligence", "asset_inventory", "application_security", "source_code"),
    "Relevance": ("vulnerability_intelligence", "asset_inventory", "application_security", "source_code"),
    "Hunt": ("siem", "edr_xdr"),
    "Detections": ("siem", "edr_xdr"),
    "Gap analysis": ("siem", "edr_xdr"),
    "Discovery": ("scanner", "easm", "asset_inventory", "source_code", "application_security"),
}

SLOT_FILLED = "filled"
SLOT_EMPTY = "empty"
SLOT_UNAVAILABLE = "unavailable"

#: The state of a capability type across the members that use it (#1201).
TypeStateName = Literal["connected", "partial", "not_bound", "not_usable", "none_connected"]
TYPE_CONNECTED: TypeStateName = "connected"
TYPE_PARTIAL: TypeStateName = "partial"
TYPE_NOT_BOUND: TypeStateName = "not_bound"
#: Nothing bound or bindable, but an integration of the type exists that the members cannot take (#1373).
TYPE_NOT_USABLE: TypeStateName = "not_usable"
TYPE_NONE_CONNECTED: TypeStateName = "none_connected"

#: Why an integration of the right type is not a candidate (#1373).
BlockedReason = Literal["not_company_managed", "not_connected"]
BLOCKED_NOT_COMPANY_MANAGED: BlockedReason = "not_company_managed"
BLOCKED_NOT_CONNECTED: BlockedReason = "not_connected"


@dataclass(frozen=True)
class BuiltinOption:
    """An option of a capability served by XTM One itself: no integration, never a binding."""

    key: str
    label: str
    #: Always active: external integrations add to it and it cannot be removed.
    always_on: bool = True


#: Capability key -> its built-in option, offered first and active by default (#1201).
BUILTIN_OPTIONS: dict[str, BuiltinOption] = {
    # The organization context assets, fed by OpenAEV, OpenCRQ, OpenCTI, Intune,
    # Defender and the other asset sources: those are sources of this inventory,
    # never alternatives offered in the slot.
    "asset_inventory": BuiltinOption(key="xtm_one_inventory", label="XTM One inventory (built in)"),
    # The bell, the decisions surface and work items; no external send.
    "notification": BuiltinOption(key="in_app", label="In XTM One only"),
}


def builtin_for(capability: str) -> BuiltinOption | None:
    """The built-in option of *capability*, if it has one."""
    return BUILTIN_OPTIONS.get(capability)


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
    """The catalog providers whose ``integration_types`` fill *capability*.

    An entry flagged ``capability_slots: False`` is never a candidate: a tool
    such as an extraction model keeps its ``integration_types`` for the
    catalog browser without being offered as a data source of a slot. A
    capability's ``excluded_types`` keep out an entry typed one of them, so a
    provider listed under a more specific capability is offered once (#1434).
    """
    types = set(CAPABILITIES[capability]["integration_types"])
    excluded = set(CAPABILITIES[capability].get("excluded_types", ()))
    return frozenset(
        key
        for key, entry in catalog.items()
        if entry.get("capability_slots", True)
        and types & set(entry.get("integration_types") or ())
        and not excluded & set(entry.get("integration_types") or ())
    )


@dataclass
class IntegrationRef:
    """An integration as a slot shows it."""

    integration_id: str
    provider: str
    name: str
    company_managed: bool = False
    #: Usable now (status ``connected``); a visible integration that is not is only ever ``blocked``.
    connected: bool = True
    #: Set on a blocked integration: why the slot cannot take it.
    reason: BlockedReason | None = None


@dataclass
class SlotState:
    capability: str
    label: str
    state: str
    bound: list[IntegrationRef] = field(default_factory=list)
    candidates: list[IntegrationRef] = field(default_factory=list)
    builtin: BuiltinOption | None = None
    #: Integrations of the capability the member cannot take, each with its ``reason`` (#1373).
    blocked: list[IntegrationRef] = field(default_factory=list)


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

    *connected* are the integrations the reader can see; one whose
    ``connected`` flag is off is never a candidate. A company-managed agent
    takes company-managed integrations only
    (``agent_bindings.validate_company_managed_bindings``), so its candidates
    are narrowed to them. What the narrowing drops is ``blocked``, with its
    reason, so a connected integration of the type is never hidden (#1373).
    """
    held = bound_ids(bindings)
    visible = list(connected)
    pool = [i for i in visible if i.connected and (i.company_managed or not company_managed_only)]
    by_id = {i.integration_id: i for i in visible}
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
        candidate_set = {c.integration_id for c in candidates}
        blocked = [
            replace(i, reason=_blocked_reason(i, company_managed_only))
            for i in visible
            if i.provider in providers and i.integration_id not in bound_set and i.integration_id not in candidate_set
        ]
        builtin = builtin_for(capability)
        # A built-in option fills the slot by itself: never "None connected".
        if bound or builtin is not None:
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
                builtin=builtin,
                blocked=blocked,
            )
        )
    return states


def _blocked_reason(integration: IntegrationRef, company_managed_only: bool) -> BlockedReason:
    """Why *integration* is not a candidate: not connected first, it is the one a person fixes by signing in."""
    if not integration.connected:
        return BLOCKED_NOT_CONNECTED
    return BLOCKED_NOT_COMPANY_MANAGED


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


def bindings_set(
    role_label: str | None,
    capability: str,
    bindings: Mapping[str, Any] | None,
    integrations: Iterable[IntegrationRef],
    catalog: Mapping[str, Any],
    *,
    company_managed_only: bool = False,
) -> dict[str, list[str]]:
    """*bindings* with exactly *integrations* bound for *capability* (#1201).

    The ids of the capability's providers that are not in *integrations* are
    unbound; the bindings of every other provider are kept as they are. Each
    integration passes the :func:`bindings_with` checks, so a refusal leaves
    nothing half written. An empty *integrations* clears the capability (the
    built-in option, when the capability has one, stays active).
    """
    _check_slot(role_label, capability)
    wanted = list(integrations)
    providers = providers_for(capability, catalog)
    held = bound_ids(bindings)
    keep = {i.integration_id for i in wanted}
    for provider in providers:
        held[provider] = {iid for iid in held.get(provider, set()) if iid in keep}
    out: Mapping[str, Any] = {provider: sorted(ids) for provider, ids in held.items() if ids}
    for integration in wanted:
        out = bindings_with(
            role_label, capability, out, integration, catalog, company_managed_only=company_managed_only
        )
    return {provider: list(ids) for provider, ids in out.items()}


@dataclass
class TypeMember:
    """One member using a capability type, as the expanded type row shows it."""

    member_id: str
    agent_id: str
    name: str
    role_label: str
    can_edit: bool
    bound: list[IntegrationRef]
    #: Its bound set is not empty and differs from the fleet choice (an override).
    #: An empty slot is not an override: a type-level choice fills it.
    custom: bool


@dataclass
class TypeState:
    """A capability across the fleet's members (#1201): one row of the type view."""

    capability: str
    label: str
    state: TypeStateName
    #: The fleet choice: the bound set most members share (first member on a tie).
    choice: list[IntegrationRef]
    candidates: list[IntegrationRef]
    members: list[TypeMember]
    builtin: BuiltinOption | None = None
    #: What no member can take, with its reason (#1373), first seen first.
    blocked: list[IntegrationRef] = field(default_factory=list)


def _ids(refs: Iterable[IntegrationRef]) -> frozenset[str]:
    return frozenset(r.integration_id for r in refs)


def fleet_choice(bound_sets: Sequence[frozenset[str]]) -> frozenset[str]:
    """The non-empty bound set most members share; on a tie the first member's (empty when none)."""
    filled = [ids for ids in bound_sets if ids]
    if not filled:
        return frozenset()
    counts: dict[frozenset[str], int] = {}
    for ids in filled:
        counts[ids] = counts.get(ids, 0) + 1
    best = max(counts.values())
    return next(ids for ids in filled if counts[ids] == best)


def type_states(members: Sequence[tuple[TypeMember, Sequence[SlotState]]]) -> list[TypeState]:
    """One :class:`TypeState` per capability the members' roles declare, in first-seen order.

    *members* pairs each member (its ``bound`` and ``custom`` are recomputed
    here per capability) with its :func:`slot_states`. The state reads
    ``connected`` when every member's slot is filled (a built-in option fills
    it), ``partial`` when some are, ``not_bound`` when none is but a connected
    integration can be bound, ``not_usable`` when none can but an integration
    of the type is blocked (#1373), ``none_connected`` otherwise.
    """
    order: list[str] = []
    per_cap: dict[str, list[tuple[TypeMember, SlotState]]] = {}
    for member, states in members:
        for slot in states:
            if slot.capability not in per_cap:
                order.append(slot.capability)
                per_cap[slot.capability] = []
            per_cap[slot.capability].append((member, slot))
    out: list[TypeState] = []
    for capability in order:
        rows = per_cap[capability]
        choice_ids = fleet_choice([_ids(slot.bound) for _, slot in rows])
        refs: dict[str, IntegrationRef] = {}
        for _, slot in rows:
            for ref in [*slot.bound, *slot.candidates]:
                if ref.name or ref.integration_id not in refs:
                    refs[ref.integration_id] = ref
        filled = sum(1 for _, slot in rows if slot.state == SLOT_FILLED)
        state: TypeStateName
        if filled == len(rows):
            state = TYPE_CONNECTED
        elif filled:
            state = TYPE_PARTIAL
        elif any(slot.state == SLOT_EMPTY for _, slot in rows):
            state = TYPE_NOT_BOUND
        elif any(slot.blocked for _, slot in rows):
            state = TYPE_NOT_USABLE
        else:
            state = TYPE_NONE_CONNECTED
        blocked: dict[str, IntegrationRef] = {}
        for _, slot in rows:
            for ref in slot.blocked:
                blocked.setdefault(ref.integration_id, ref)
        out.append(
            TypeState(
                capability=capability,
                label=CAPABILITIES[capability]["label"],
                state=state,
                choice=[refs[iid] for iid in sorted(choice_ids)],
                candidates=sorted(refs.values(), key=lambda r: (r.name or r.provider).lower()),
                members=[
                    TypeMember(
                        member_id=member.member_id,
                        agent_id=member.agent_id,
                        name=member.name,
                        role_label=member.role_label,
                        can_edit=member.can_edit,
                        bound=list(slot.bound),
                        custom=bool(slot.bound) and _ids(slot.bound) != choice_ids,
                    )
                    for member, slot in rows
                ],
                builtin=builtin_for(capability),
                blocked=list(blocked.values()),
            )
        )
    return out


def members_to_write(state: Any, *, override_custom: bool) -> list[str]:
    """The member ids a type-level choice writes (#1201).

    Every member the reader may edit, except a ``custom`` one, which keeps its
    override unless the person confirmed (*override_custom*). *state* is a
    :class:`TypeState` or its API model (same ``members`` fields).
    """
    return [m.member_id for m in state.members if m.can_edit and (override_custom or not m.custom)]


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
