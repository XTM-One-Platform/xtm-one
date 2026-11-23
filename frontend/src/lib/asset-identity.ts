// SPDX-License-Identifier: Apache-2.0

/**
 * The asset identity settings of CTEM > Inventory > Sources (#1178 slice 3): which strong identifier
 * merges the records of several sources into one asset, most reliable first, and which source each
 * field shows. Pure, so the card and its tests share it. The backend holds the defaults
 * (`GET /organization-context/assets/identity`); a saved value equal to its default is not stored,
 * so a later change of the defaults still reaches an organization that never changed them.
 */

import type { AssetIdentitySettings } from "./organization-profile";

/** The strong identifier kinds a person can order (the backend's DEFAULT_KEY_ORDER). @i18n-keys */
export const IDENTITY_KEY_LABELS: Readonly<Record<string, string>> = {
  serial: "Serial number",
  cloud_resource_id: "Cloud resource ID",
  repository: "Repository",
  device_id: "Device ID",
  mac: "MAC address",
  fqdn: "FQDN",
  hostname: "Hostname",
} as const;

/** The fields whose source is chosen by precedence. @i18n-keys */
export const PRECEDENCE_FIELD_LABELS: Readonly<Record<string, string>> = {
  os_family: "Operating system",
  os_version: "OS version",
  os_build: "OS build",
  software: "Software",
  criticality: "Criticality",
  internet_facing: "Internet facing",
  category: "Category",
  subcategory: "Subcategory",
} as const;

/** The source classes that are not a platform name. @i18n-keys */
export const SOURCE_CLASS_LABELS: Readonly<Record<string, string>> = {
  hand: "Typed by a person",
  agent: "Agent",
  mdm: "Endpoint manager",
  edr: "EDR",
  "*": "Any other source",
} as const;

/** The list with the item at *index* moved by *delta* places; out of range leaves it unchanged. */
export function moveItem<T>(list: readonly T[], index: number, delta: -1 | 1): T[] {
  const target = index + delta;
  if (index < 0 || index >= list.length || target < 0 || target >= list.length) return [...list];
  const next = [...list];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

const sameList = (a: readonly string[], b: readonly string[]) => a.length === b.length && a.every((item, i) => item === b[i]);

/** The draft being edited: every key with whether it merges, and the effective order of each field. */
export interface IdentityDraft {
  keys: { key: string; on: boolean }[];
  precedence: Record<string, string[]>;
}

/** The draft from the effective settings: the keys in effect first, in their order, then the ones left out. */
export function draftOf(effective: AssetIdentitySettings, defaults: AssetIdentitySettings): IdentityDraft {
  const all = [...effective.key_order, ...defaults.key_order.filter((key) => !effective.key_order.includes(key))];
  return {
    keys: all.map((key) => ({ key, on: effective.key_order.includes(key) })),
    precedence: Object.fromEntries(Object.entries(effective.precedence).map(([field, order]) => [field, [...order]])),
  };
}

/**
 * What to save: the key order only when it differs from the default (else empty, the default), and
 * only the fields whose order differs from their default.
 */
export function savedOf(draft: IdentityDraft, defaults: AssetIdentitySettings): AssetIdentitySettings {
  const order = draft.keys.filter((entry) => entry.on).map((entry) => entry.key);
  const precedence: Record<string, string[]> = {};
  for (const [field, list] of Object.entries(draft.precedence)) {
    if (!sameList(list, defaults.precedence[field] ?? [])) precedence[field] = list;
  }
  return { key_order: sameList(order, defaults.key_order) ? [] : order, precedence };
}

/** Whether no key merges: every record would stay an asset of its own. */
export function noKeyOn(draft: IdentityDraft): boolean {
  return !draft.keys.some((entry) => entry.on);
}
