/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * Pure logic of the fleet Members view's Capabilities table: one row per
 * capability type the fleet needs (#1201; one row per member x slot before,
 * #1128), the icon of each capability kind, the status order that puts what
 * is left to bind first, the fleet choice and the per-member overrides, and
 * the default open state of the two cards above the Members table
 * (Capabilities open, Fleet defaults folded).
 */
import type { IconName } from "@filigran/design-system";

import type { FleetCapabilityType, FleetCapabilityTypeMember, FleetSlotIntegration } from "@/hooks/api/useFleetApi";

/** Capabilities is what a fleet setup needs first: open on arrival. */
export const CAPABILITIES_OPEN_BY_DEFAULT = true;
/** Fleet defaults are secondary: one summary bar until the user opens it. */
export const FLEET_DEFAULTS_OPEN_BY_DEFAULT = false;

/** The words of a type row's state, short and plain (13bis.10). @i18n-keys */
export const TYPE_STATE_LABELS = {
  connected: "Connected",
  partial: "Partial",
  not_bound: "Not bound",
  none_connected: "None connected",
} as const satisfies Record<FleetCapabilityType["state"], string>;

/** What can be bound but is not reads as a warning, apart from "None connected" (UX r1 A1 of #994). */
export const TYPE_STATE_VARIANTS = {
  connected: "success",
  partial: "warning",
  not_bound: "warning",
  none_connected: "outline",
} as const satisfies Record<FleetCapabilityType["state"], string>;

/** What is left to do first: Not bound, Partial, None connected, then Connected. */
const TYPE_STATE_ORDER: Record<FleetCapabilityType["state"], number> = {
  not_bound: 0,
  partial: 1,
  none_connected: 2,
  connected: 3,
};

/** The capabilities that take several integrations at once: the picker is a multi-select (#1201). */
export const MULTI_CHANNEL_CAPABILITIES: ReadonlySet<string> = new Set(["notification"]);

/** The picker value of a capability's built-in option (never an integration id). */
const BUILTIN_VALUE_PREFIX = "builtin:";

/**
 * One design-system icon per capability of the backend catalog
 * (`backend/app/services/fleet_capability_slots.py`, CAPABILITIES).
 */
export const CAPABILITY_ICONS: Record<string, IconName> = {
  ticketing: "clipboard-list",
  notification: "bell",
  edr_xdr: "shield-check",
  scanner: "bug",
  easm: "compass",
  vulnerability_intelligence: "triangle-alert",
  asset_inventory: "server",
  siem: "activity",
};

/** The icon of a capability the catalog adds before this map knows it. */
export const FALLBACK_CAPABILITY_ICON: IconName = "network";

export function capabilityIcon(capability: string): IconName {
  return CAPABILITY_ICONS[capability] ?? FALLBACK_CAPABILITY_ICON;
}

/** The sort value of the Status column: ascending puts Not bound first. */
export function typeStateRank(type: FleetCapabilityType): number {
  return TYPE_STATE_ORDER[type.state];
}

/** The text a search reads on a type row: capability, its members and roles, and every bound integration. */
export function typeSearchFields(type: FleetCapabilityType, label: string): string[] {
  return [
    label,
    type.capability,
    type.builtin?.label ?? "",
    ...type.members.flatMap((member) => [
      member.name,
      member.role_label,
      ...member.bound.flatMap((integration) => [integration.name, integration.provider]),
    ]),
  ];
}

/** Connected capability types and every type, for the header summary. */
export function typeCounts(types: readonly FleetCapabilityType[]): { connected: number; total: number } {
  return { connected: types.filter((type) => type.state === "connected").length, total: types.length };
}

/** The members of a type with an override of their own ("Custom"). */
export function customMembers(type: FleetCapabilityType): FleetCapabilityTypeMember[] {
  return type.members.filter((member) => member.custom);
}

/** Whether the reader may change the type at all: at least one member they may edit. */
export function typeEditable(type: FleetCapabilityType): boolean {
  return type.members.some((member) => member.can_edit);
}

/** The custom members a type-level write would overwrite if the person confirms: the editable ones. */
export function overwritableCustomMembers(type: FleetCapabilityType): FleetCapabilityTypeMember[] {
  return customMembers(type).filter((member) => member.can_edit);
}

/** The ids of a bound set, sorted, so two sets compare by value. */
export function integrationIds(integrations: readonly FleetSlotIntegration[]): string[] {
  return integrations.map((integration) => integration.integration_id).sort();
}

/** The picker value of the built-in option of *type* (empty when it has none). */
export function builtinValue(type: Pick<FleetCapabilityType, "builtin">): string {
  return type.builtin ? `${BUILTIN_VALUE_PREFIX}${type.builtin.key}` : "";
}

/**
 * The integrations a single-choice pick writes: the built-in option and an
 * emptied field write none (the built-in option alone, or nothing), any other
 * value is that one integration.
 */
export function idsForPick(value: string): string[] {
  return !value || value.startsWith(BUILTIN_VALUE_PREFIX) ? [] : [value];
}

/**
 * The next set of a multi-channel pick after one toggle: the built-in option
 * is always on and never toggled off, an integration is added or removed.
 */
export function toggledIds(current: readonly string[], value: string): string[] {
  if (value.startsWith(BUILTIN_VALUE_PREFIX)) return [...current].sort();
  return (current.includes(value) ? current.filter((id) => id !== value) : [...current, value]).sort();
}

/** The value a single-choice picker shows: the bound integration, else the built-in option, else nothing. */
export function pickerValue(type: Pick<FleetCapabilityType, "builtin">, bound: readonly FleetSlotIntegration[]): string {
  return bound[0]?.integration_id ?? builtinValue(type);
}

/** The instruction part of the folded Fleet defaults summary. @i18n-keys */
export const DEFAULTS_INSTRUCTION_SUMMARY = {
  set: "Collaboration instruction set",
  none: "No collaboration instruction",
} as const;

/** Which instruction words the folded summary shows: a blank instruction reads as none. */
export function instructionSummaryKey(instruction: string | null | undefined): keyof typeof DEFAULTS_INSTRUCTION_SUMMARY {
  return (instruction ?? "").trim() ? "set" : "none";
}
