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

import type {
  FleetCapabilityType,
  FleetCapabilityTypeMember,
  FleetSlotBlockedIntegration,
  FleetSlotIntegration,
} from "@/hooks/api/useFleetApi";

/** Capabilities is what a fleet setup needs first: open on arrival. */
export const CAPABILITIES_OPEN_BY_DEFAULT = true;
/** Fleet defaults are secondary: one summary bar until the user opens it. */
export const FLEET_DEFAULTS_OPEN_BY_DEFAULT = false;

/** The words of a type row's state, short and plain (13bis.10). @i18n-keys */
export const TYPE_STATE_LABELS = {
  connected: "Connected",
  partial: "Partial",
  not_bound: "Not bound",
  not_usable: "Not usable",
  none_connected: "None connected",
} as const satisfies Record<FleetCapabilityType["state"], string>;

/** What can be bound but is not reads as a warning, apart from "None connected" (UX r1 A1 of #994). */
export const TYPE_STATE_VARIANTS = {
  connected: "success",
  partial: "warning",
  not_bound: "warning",
  // Something of the type exists but cannot be taken: a person has a fix to make (#1373).
  not_usable: "warning",
  none_connected: "outline",
} as const satisfies Record<FleetCapabilityType["state"], string>;

/**
 * The longest state label of the four catalogs, in characters ("Ninguna conectada", es), and the
 * Status column sized from it: 6.9 px a character at the badge's 12 px (measured: "Aucune connectée",
 * 16 characters, is a 126 px badge), the badge's 8 px padding a side and the cell's 12 px a side
 * (UX r1 B5 and r2 B1 of #1209). capability-slots-table.test.ts fails when a catalog grows a longer label.
 */
export const STATUS_LABEL_MAX_CHARS = 17;
export const STATUS_COLUMN_WIDTH = Math.ceil(STATUS_LABEL_MAX_CHARS * 6.9) + 16 + 24;

/** The Members column: "# members" in the four catalogs. */
export const MEMBERS_COLUMN_WIDTH = 112;
/** The row actions cell: the type picker's 240 px box and 12 px a side. */
export const ROW_ACTIONS_WIDTH = 264;
/**
 * Below this table width (the 7xl container) the Members and Integration columns step aside to
 * the Columns pill, as the cases table does, so capability names keep their room at 1080 and in
 * the setup dialog (UX r2 B1 of #1209): the type picker still shows the fleet choice and the
 * opened row lists the members.
 */
export const SECONDARY_COLUMNS_HIDE_BELOW = "7xl" as const;
export const SECONDARY_COLUMNS_HIDE_BELOW_PX = 1280;
/** What a capability cell spends beside its name: two 16 px icons, two 8 px gaps, 12 px padding a side. */
export const CAPABILITY_CELL_CHROME = 16 * 2 + 8 * 2 + 24;

/**
 * The width the fixed layout leaves each flexible column (Capability, and Integration when drawn)
 * at a table width: what Status, the row actions and, when drawn, Members leave, shared.
 */
export function flexibleColumnWidth(tableWidth: number): number {
  const wide = tableWidth >= SECONDARY_COLUMNS_HIDE_BELOW_PX;
  const left = tableWidth - STATUS_COLUMN_WIDTH - ROW_ACTIONS_WIDTH - (wide ? MEMBERS_COLUMN_WIDTH : 0);
  return wide ? left / 2 : left;
}

/** What is left to do first: Not bound, Partial, Not usable, None connected, then Connected. */
const TYPE_STATE_ORDER: Record<FleetCapabilityType["state"], number> = {
  not_bound: 0,
  partial: 1,
  not_usable: 2,
  none_connected: 3,
  connected: 4,
};

/**
 * Why an integration of the type cannot be taken, and how to fix it, one line a reason (#1373):
 * the row of a Not usable type says it instead of "Connect one". @i18n-keys
 */
export const BLOCKED_REASON_LINES = {
  not_company_managed:
    "{name} is connected but not company-managed: this fleet's members take company-managed integrations only. An administrator can make it company-managed.",
  not_connected: "{name} is not connected yet: finish its sign-in on the integration page.",
} as const satisfies Record<FleetSlotBlockedIntegration["reason"], string>;

/** The short words of a blocked integration in the row's action cell (#1373). @i18n-keys */
export const BLOCKED_REASON_SHORT = {
  not_company_managed: "Not company-managed",
  not_connected: "Sign-in not finished",
} as const satisfies Record<FleetSlotBlockedIntegration["reason"], string>;

/** The integration a Not usable row names first: one the members could take after a fix, else none. */
export function firstBlocked(type: Pick<FleetCapabilityType, "blocked">): FleetSlotBlockedIntegration | null {
  return type.blocked?.[0] ?? null;
}

/** The page that fixes a blocked integration: its own detail page (#1373). */
export function blockedFixPath(blocked: Pick<FleetSlotBlockedIntegration, "integration_id">): string {
  return `/integrations/${blocked.integration_id}`;
}

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
  application_security: "code-xml",
  source_code: "file-code-corner",
  easm: "compass",
  vulnerability_intelligence: "triangle-alert",
  asset_inventory: "server",
  siem: "activity",
  search_engine: "earth",
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
 * The next set of a capability pick after one toggle (every capability takes several integrations,
 * #2424): the built-in option is always on and never toggled off, an integration is added or
 * removed, the others stay bound.
 */
export function toggledIds(current: readonly string[], value: string): string[] {
  if (value.startsWith(BUILTIN_VALUE_PREFIX)) return [...current].sort();
  return (current.includes(value) ? current.filter((id) => id !== value) : [...current, value]).sort();
}

/**
 * The values a capability picker shows checked (#1373): the always-on built-in option first,
 * then the bound channels. The built-in is a locked row of the list, never a field placeholder.
 */
export function multiChannelSelection(
  type: Pick<FleetCapabilityType, "builtin">,
  bound: readonly FleetSlotIntegration[],
): string[] {
  const builtin = builtinValue(type);
  return [...(builtin ? [builtin] : []), ...integrationIds(bound)];
}

/** Whether a picker value is a capability's built-in option. */
export function isBuiltinValue(value: string): boolean {
  return value.startsWith(BUILTIN_VALUE_PREFIX);
}

/** The members of a type whose slot nothing fills: no integration bound and no built-in option. */
export function unsetMembers(type: FleetCapabilityType): FleetCapabilityTypeMember[] {
  return type.builtin ? [] : type.members.filter((member) => member.bound.length === 0);
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
