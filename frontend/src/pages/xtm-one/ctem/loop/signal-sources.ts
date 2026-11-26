// SPDX-License-Identifier: Apache-2.0

/**
 * The signal sources dialog's rules (#1189, dev-docs/ctem-signals.md section
 * 2bis): the status of a trigger path, where its Connect action goes, and the
 * icon of each path and lens. Pure, so the status derivation is unit-tested.
 */
import type { CtemSignalPath, CtemSignalPathSource, CtemSignalPathStatus } from "@/hooks/api/useCtemSignalsApi";
import type { SignalSourcesSettings } from "@/lib/organization-profile";
import { profileFieldHref } from "@/lib/organization-profile-tabs";

/** The status labels. @i18n-keys */
export const PATH_STATUS_LABEL = {
  enabled: "Enabled",
  partly_enabled: "Partly enabled",
  error: "Error",
  not_set_up: "Not set up",
  off: "Off",
  not_connected: "Not connected",
  agents_api: "Agents and API only",
} as const satisfies Record<CtemSignalPathStatus, string>;

/**
 * Enabled reads healthy, partly enabled and a source left unbound read medium, an error reads critical,
 * a source not set up, a source turned off and agents-only are neutral: never green.
 */
export const PATH_STATUS_TONE = {
  enabled: "low",
  partly_enabled: "medium",
  error: "critical",
  not_set_up: "neutral",
  off: "neutral",
  not_connected: "medium",
  agents_api: "neutral",
} as const satisfies Record<CtemSignalPathStatus, "low" | "medium" | "neutral" | "critical">;

/** The status order of the sort and the filter: enabled first, agents-only last. */
export const PATH_STATUS_ORDER = ["enabled", "partly_enabled", "error", "not_set_up", "off", "not_connected", "agents_api"] as const satisfies readonly CtemSignalPathStatus[];

/** The sources of a path in error, summed over its integrations (#2434). */
function pathErrorCount(path: Pick<CtemSignalPath, "sources">): number {
  return (path.sources ?? []).reduce((n, s) => n + (s.error_count ?? 0), 0);
}

type Translate = (key: string, values?: Record<string, string | number>) => string;

/** A path's status in words; an error says how many sources are in error ("1 source in error"). */
export function pathStatusLabel(path: Pick<CtemSignalPath, "status" | "sources">, t_i18n: Translate): string {
  const status = pathStatus(path);
  const errors = pathErrorCount(path);
  if (status === "error" && errors > 0) return t_i18n("{count, plural, one {# source in error} other {# sources in error}}", { count: errors });
  return t_i18n(PATH_STATUS_LABEL[status]);
}

/**
 * A path's status: the API's when present, else derived from its sources the same way (no source:
 * agents and the API only; one in error: error; every used source on: enabled; some on: partly
 * enabled; one not set up: not set up; one off: off). One status per path, the one its sources say
 * (UX round 1 on #2442, B1), never the integration connection alone (#2436, Sam 26 Nov 2026).
 */
export function pathStatus(path: Pick<CtemSignalPath, "status" | "sources">): CtemSignalPathStatus {
  if (path.status) return path.status;
  const sources = path.sources ?? [];
  if (sources.length === 0) return "agents_api";
  if (sources.some((s) => (s.error_count ?? 0) > 0)) return "error";
  if (sources.some((s) => s.bound)) {
    // A source taken off the path, or of a provider with no integration here, never makes it partial.
    const used = sources.filter((s) => !s.excluded && (s.bound || s.off || s.not_set_up));
    return used.every((s) => s.bound) ? "enabled" : "partly_enabled";
  }
  if (sources.some((s) => s.not_set_up)) return "not_set_up";
  return sources.some((s) => s.off) ? "off" : "not_connected";
}

/** A source taken off this path still counts as connected for its provider: it feeds other paths (#1445). */
function liveOnPlatform(s: CtemSignalPathSource): boolean {
  return s.bound || Boolean(s.off) || Boolean(s.available) || Boolean(s.not_set_up) || (s.error_count ?? 0) > 0;
}

/**
 * The providers connected on this platform: those with a bound source or a pass turned off on any
 * path (a pass turned off exists only on a connected provider). Read once over every path.
 */
export function connectedProviders(paths: readonly Pick<CtemSignalPath, "sources">[]): Set<string> {
  return new Set(paths.flatMap((p) => (p.sources ?? []).filter(liveOnPlatform).map((s) => s.provider)));
}

/**
 * One source's state on this platform (UX round 1 on #1405, B1): bound, a pass turned off, not set
 * up (its provider is connected, the source itself is not, e.g. the PIR playbook of a connected
 * OpenCTI), or not connected (no integration of its provider here).
 */
type SourceState = "error" | "bound" | "excluded" | "off" | "not_set_up" | "not_connected";

export function sourceState(source: CtemSignalPathSource, connected: ReadonlySet<string>): SourceState {
  // A source XTM One provisions in error on one of its integrations reads Error, as its platform line does (#2442 UX r1 B1).
  if ((source.error_count ?? 0) > 0) return "error";
  if (source.bound) return "bound";
  if (source.excluded) return "excluded";
  if (source.off) return "off";
  return connected.has(source.provider) ? "not_set_up" : "not_connected";
}

/**
 * A path's sources split by what feeds it on this platform (Sam, 23 Nov 2026, #1401): `live` are the
 * sources of a connected provider (bound, a pass turned off, or not set up yet), each saying its own
 * state; `possible` are the sources whose provider has no integration here. A possible source is never
 * listed with the weight of a live one: the Sources tab folds them into one muted "+N possible".
 */
export function splitSources(
  path: Pick<CtemSignalPath, "sources">,
  connected: ReadonlySet<string>,
): { live: CtemSignalPathSource[]; possible: CtemSignalPathSource[] } {
  const sources = path.sources ?? [];
  const possible = (s: CtemSignalPathSource) => sourceState(s, connected) === "not_connected";
  return { live: sources.filter((s) => !possible(s)), possible: sources.filter(possible) };
}

/** Whether a path records from at least one of its sources: enabled or partly enabled. */
export function isLiveStatus(status: CtemSignalPathStatus): boolean {
  return status === "enabled" || status === "partly_enabled";
}

/** "N of M trigger paths connected": a path with a source on counts in N; agents-only paths count in M, never in N. */
export function connectedCount(paths: readonly Pick<CtemSignalPath, "status" | "sources">[]): number {
  return paths.filter((p) => isLiveStatus(pathStatus(p))).length;
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
  // A source an administrator excluded from the path is a choice, never a source to connect.
  const unbound = (path.sources ?? []).filter((s) => !s.bound && !s.excluded);
  const off = unbound.find((s) => s.off);
  if (off) return off;
  const order = ["listener", "pass", "pir_playbook"];
  return [...unbound].sort((a, b) => order.indexOf(a.kind) - order.indexOf(b.kind))[0] ?? null;
}

/** The key of one source in the profile's `signal_sources.excluded` (#1445): `provider:kind`. */
export function sourceKey(source: Pick<CtemSignalPathSource, "provider" | "kind">): string {
  return `${source.provider}:${source.kind}`;
}

/**
 * The `signal_sources` of the profile with one source taken off (`excluded` true) or put back on one
 * path (#1445): the other paths and sources are kept, an empty path list is dropped, keys sorted
 * like the backend stores them.
 */
export function withExclusion(
  current: SignalSourcesSettings | undefined,
  path: string,
  key: string,
  excluded: boolean,
): SignalSourcesSettings {
  const all: Record<string, string[]> = { ...(current?.excluded ?? {}) };
  const keys = new Set(all[path] ?? []);
  if (excluded) keys.add(key);
  else keys.delete(key);
  if (keys.size > 0) all[path] = [...keys].sort();
  else delete all[path];
  return { excluded: all };
}

/**
 * Whether a source can be taken off one path, or put back on it (#1445): a connected source (bound
 * before the exclusion) or one already excluded. A source that is not connected has nothing to exclude.
 */
export function canExclude(source: CtemSignalPathSource): boolean {
  return Boolean(source.available || source.excluded);
}

/** The profile field the switch of a profile pass writes (`enabled`), or null for any other source. */
export function profilePassField(source: Pick<CtemSignalPathSource, "provider" | "kind">): "attack_surface" | "evidence_health" | null {
  if (source.kind !== "pass") return null;
  const field = PROFILE_PASS_FIELD[source.provider];
  return field === "attack_surface" || field === "evidence_health" ? field : null;
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
