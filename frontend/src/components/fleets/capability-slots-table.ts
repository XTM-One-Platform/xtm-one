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
 * Pure logic of the fleet Members view's Capabilities table (#1128): one row
 * per member x capability slot, the icon of each capability kind, the status
 * order that puts what is left to bind first, and the default open state of
 * the two cards above the Members table (Capabilities open, Fleet defaults
 * folded).
 */
import type { IconName } from "@filigran/design-system";

import type { FleetCapabilitySlot, FleetMemberSlots } from "@/hooks/api/useFleetApi";

/** Capabilities is what a fleet setup needs first: open on arrival. */
export const CAPABILITIES_OPEN_BY_DEFAULT = true;
/** Fleet defaults are secondary: one summary bar until the user opens it. */
export const FLEET_DEFAULTS_OPEN_BY_DEFAULT = false;

/** The words of a slot's state, short and plain (13bis.10). @i18n-keys */
export const SLOT_STATE_LABELS = {
  filled: "Connected",
  empty: "Not bound",
  unavailable: "None connected",
} as const satisfies Record<FleetCapabilitySlot["state"], string>;

/** A slot that can be bound but is not reads as a warning, apart from "None connected" (UX r1 A1 of #994). */
export const SLOT_STATE_VARIANTS = {
  filled: "success",
  empty: "warning",
  unavailable: "outline",
} as const satisfies Record<FleetCapabilitySlot["state"], string>;

/** What is left to do first: Not bound, then None connected, then Connected. */
const SLOT_STATE_ORDER: Record<FleetCapabilitySlot["state"], number> = {
  empty: 0,
  unavailable: 1,
  filled: 2,
};

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

/** One row of the Capabilities table: a member and one of its slots. */
export interface CapabilitySlotRow {
  key: string;
  member: FleetMemberSlots;
  slot: FleetCapabilitySlot;
}

/** Every member x slot, in the API order (members, then each member's slots). */
export function capabilitySlotRows(members: readonly FleetMemberSlots[]): CapabilitySlotRow[] {
  return members.flatMap((member) =>
    member.slots.map((slot) => ({ key: `${member.member_id}:${slot.capability}`, member, slot })),
  );
}

/** The sort value of the Status column: ascending puts Not bound first. */
export function slotStateRank(row: CapabilitySlotRow): number {
  return SLOT_STATE_ORDER[row.slot.state];
}

/** The text a search reads on a row: capability, member, role and every bound integration. */
export function slotRowSearchFields(row: CapabilitySlotRow, label: string): string[] {
  return [
    label,
    row.slot.capability,
    row.member.name,
    row.member.role_label,
    ...row.slot.bound.flatMap((integration) => [integration.name, integration.provider]),
  ];
}

/** Connected slots and every slot, for the header summary. */
export function slotCounts(members: readonly FleetMemberSlots[]): { connected: number; total: number } {
  const slots = members.flatMap((member) => member.slots);
  return { connected: slots.filter((slot) => slot.state === "filled").length, total: slots.length };
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

/** The distinct capabilities of the rows, in first-seen order, for the capability filter. */
export function capabilityOptions(rows: readonly CapabilitySlotRow[]): { value: string; label: string }[] {
  const seen = new Map<string, string>();
  for (const row of rows) if (!seen.has(row.slot.capability)) seen.set(row.slot.capability, row.slot.label);
  return [...seen].map(([value, label]) => ({ value, label }));
}
