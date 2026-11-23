// SPDX-License-Identifier: Apache-2.0

/**
 * The signal sources dialog's rules (#1189, dev-docs/ctem-signals.md section
 * 2bis): the status of a trigger path, where its Connect action goes, and the
 * icon of each path and lens. Pure, so the status derivation is unit-tested.
 */
import type { CtemSignalPath, CtemSignalPathSource, CtemSignalPathStatus } from "@/hooks/api/useCtemSignalsApi";
import { profileFieldHref } from "@/lib/organization-profile-tabs";

/** The URL query parameter that opens the dialog, on any page that mounts it. */
export const SIGNAL_SOURCES_PARAM = "signal-sources";

/** The status labels. @i18n-keys */
export const PATH_STATUS_LABEL = {
  connected: "Connected",
  off: "Off",
  not_connected: "Not connected",
  agents_api: "Agents and API only",
} as const satisfies Record<CtemSignalPathStatus, string>;

/** Connected reads healthy, a source left unbound reads medium, a pass turned off and agents-only are neutral: never green. */
export const PATH_STATUS_TONE = {
  connected: "low",
  off: "neutral",
  not_connected: "medium",
  agents_api: "neutral",
} as const satisfies Record<CtemSignalPathStatus, "low" | "medium" | "neutral">;

/**
 * A path's status: the API's when present, else derived from its sources the
 * same way (no source: agents and the API only; one bound: connected; one off: off).
 */
export function pathStatus(path: Pick<CtemSignalPath, "status" | "sources">): CtemSignalPathStatus {
  if (path.status) return path.status;
  const sources = path.sources ?? [];
  if (sources.length === 0) return "agents_api";
  if (sources.some((s) => s.bound)) return "connected";
  return sources.some((s) => s.off) ? "off" : "not_connected";
}

/**
 * A path's sources split by what feeds it on this platform (Sam, 23 Nov 2026, #1401): `live` are the
 * bound sources and the passes turned off (their provider is connected, Turn on is one click);
 * `possible` are the sources whose provider has no integration here. A possible source is never
 * listed with the weight of a live one: the Sources tab folds them into one muted "+N possible".
 */
export function splitSources(path: Pick<CtemSignalPath, "sources">): { live: CtemSignalPathSource[]; possible: CtemSignalPathSource[] } {
  const sources = path.sources ?? [];
  return { live: sources.filter((s) => s.bound || s.off), possible: sources.filter((s) => !s.bound && !s.off) };
}

/** "N of M trigger paths connected": agents-only paths count in M, never in N. */
export function connectedCount(paths: readonly Pick<CtemSignalPath, "status" | "sources">[]): number {
  return paths.filter((p) => pathStatus(p) === "connected").length;
}

/** The providers whose scheduled pass is switched on in Settings > Organization (outside lens, evidence health). */
const PROFILE_PASS_FIELD: Record<string, string> = { shodan: "attack_surface", gti: "attack_surface", opencrq: "evidence_health" };

/**
 * Whether Connect or Turn on for this source edits the organization profile (a profile
 * pass, the PIR playbook): an administrator's action, so a reader gets no button for it
 * (UX round 1 on #1356, B3).
 */
export function connectNeedsAdmin(source: Pick<CtemSignalPathSource, "provider" | "kind">): boolean {
  return source.kind === "pir_playbook" || (source.kind === "pass" && Boolean(PROFILE_PASS_FIELD[source.provider]));
}

/**
 * Where Connect goes for one source (split-sync rule): a pass set in the
 * organization profile opens its card there, the PIR playbook opens the PIR
 * field, any other source opens the new-integration page on its provider.
 */
export function connectHref(source: Pick<CtemSignalPathSource, "provider" | "kind">): string {
  if (source.kind === "pir_playbook") return profileFieldHref("pir_ids");
  if (source.kind === "pass" && PROFILE_PASS_FIELD[source.provider]) return profileFieldHref(PROFILE_PASS_FIELD[source.provider]);
  return `/integrations/new?provider=${encodeURIComponent(source.provider)}`;
}

/**
 * The first source a path should connect: a pass turned off first (Turn on is one click),
 * then a listener, then a pass, then the playbook.
 */
export function connectTarget(path: Pick<CtemSignalPath, "sources">): CtemSignalPathSource | null {
  const unbound = (path.sources ?? []).filter((s) => !s.bound);
  const off = unbound.find((s) => s.off);
  if (off) return off;
  const order = ["listener", "pass", "pir_playbook"];
  return [...unbound].sort((a, b) => order.indexOf(a.kind) - order.indexOf(b.kind))[0] ?? null;
}

/** One icon per trigger path (design-system names); a path the table does not know takes the signal icon. */
export const PATH_ICON: Record<string, string> = {
  new_vulnerability: "bug",
  relevant_threat: "custom/intrusion-set",
  code_flaw: "code-xml",
  proven_exploitable: "custom/target",
  new_exposure: "router",
  stale_asset: "unlink",
  surface_drift: "radar",
  mitigation_regression: "shield",
  control_decay: "clipboard-check",
  credential_leak: "key-round",
  third_party_event: "briefcase-business",
  blind_spot: "eye-off",
};

export const LENS_ICON = { inside: "building-2", outside: "earth", world: "custom/globe-line" } as const;

/** The source kinds in words. @i18n-keys */
export const SOURCE_KIND_LABEL = {
  listener: "Live listener",
  pass: "Scheduled pass",
  pir_playbook: "PIR playbook",
} as const satisfies Record<CtemSignalPathSource["kind"], string>;
