// SPDX-License-Identifier: Apache-2.0

/**
 * Where a row of the organization profile came from (#1134): `source` is `manual`, `csv`,
 * `api`, `agent:<agent id>` or a platform (`opencti`, `openaev`, `opencrq`); `external_id`
 * is the platform's id and `sync_id` the sync that last wrote it. A platform's row is owned
 * by its sync: read-only in the form, but for the fields the source never sends (local
 * enrichment, kept across runs by the import). Pure, so the page and its tests share it.
 * Mirrors `is_platform_source` and `SOURCED_FIELDS` of the backend.
 */

export interface Provenance {
  source?: string;
  external_id?: string;
  sync_id?: string;
}

/** The sources of rows a person wrote. */
const HAND_SOURCES = new Set(["", "manual", "csv", "api"]);
const AGENT_PREFIX = "agent:";

export type SourceKind = "manual" | "csv" | "api" | "agent" | "platform";

const norm = (source: string | undefined) => (source ?? "").trim().toLowerCase();

/** Whether a row of *source* belongs to a platform (a sync owns it) rather than to a person or an agent. */
export function isPlatformSource(source: string | undefined): boolean {
  const value = norm(source);
  return !HAND_SOURCES.has(value) && !value.startsWith(AGENT_PREFIX);
}

export function sourceKind(source: string | undefined): SourceKind {
  const value = norm(source);
  if (value.startsWith(AGENT_PREFIX)) return "agent";
  if (value === "csv" || value === "api") return value;
  if (HAND_SOURCES.has(value)) return "manual";
  return "platform";
}

/** The platform key of a platform row (its logo), else "". */
export function sourcePlatform(source: string | undefined): string {
  return isPlatformSource(source) ? norm(source) : "";
}

/** The Source labels a table and its filter draw for the hand and agent kinds. @i18n-keys */
export const SOURCE_KIND_LABELS = {
  manual: "Typed",
  csv: "CSV",
  api: "API",
  agent: "Agent",
} as const;

/**
 * The fields of a synced row a person may still set (the source never sends them): kept
 * across runs as local enrichment. Same lists as the backend's SOURCED_FIELDS.
 */
export const LOCAL_FIELDS = {
  crown_jewels: ["owner", "business_service", "assets"],
  tech_stack: ["vendor", "cpe", "version"],
  ai_systems: ["owner"],
} as const satisfies Record<string, readonly string[]>;

/** Whether the column *key* of a row of *source* takes an edit: always for a hand row, only a local field for a platform row. */
export function columnEditable(source: string | undefined, key: string, local: readonly string[]): boolean {
  return !isPlatformSource(source) || local.includes(key);
}

/** How many of *values* are platform rows (the bulk Remove names them). */
export function syncedCount(values: readonly Provenance[]): number {
  return values.filter((value) => isPlatformSource(value.source)).length;
}

/** The source ids to exclude from each sync, grouped by sync id (rows without a sync or an id are left out). */
export function exclusionsBySync(values: readonly Provenance[]): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const value of values) {
    if (!isPlatformSource(value.source) || !value.sync_id || !value.external_id) continue;
    out.set(value.sync_id, [...(out.get(value.sync_id) ?? []), value.external_id]);
  }
  return out;
}
