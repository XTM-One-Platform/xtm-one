// SPDX-License-Identifier: Apache-2.0

/**
 * The platform signal sources' rules (#2434, dev-docs/ctem-signals.md section 4bis): which
 * sources XTM One provisions in their platform, their status tone, and their reason in words.
 * Pure, so the mapping is unit-tested.
 */
import type {
  CtemPlatformSource,
  CtemPlatformSourceObject,
  CtemPlatformSourceReasonCode,
  CtemPlatformSourceStatus,
  CtemSignalPathSource,
} from "@/hooks/api/useCtemSignalsApi";

/** The `provider:kind` sources XTM One provisions on enable (`ctem_platform_sources.PROVISIONED`). */
const PROVISIONED_SOURCES: ReadonlySet<string> = new Set(["openaev:listener", "opencti:listener", "opencti:pir_playbook"]);

/** Whether a source of a path is switched on, with a run-as, as a platform source. */
export function isProvisioned(source: Pick<CtemSignalPathSource, "provider" | "kind">): boolean {
  return PROVISIONED_SOURCES.has(`${source.provider}:${source.kind}`);
}

/** The status in words. @i18n-keys */
export const PLATFORM_SOURCE_STATUS_LABEL = {
  off: "Off",
  connected: "Connected",
  error: "Error",
  not_configured: "Not configured",
} as const satisfies Record<CtemPlatformSourceStatus, string>;

/** Connected reads healthy, an error reads critical, off is neutral. */
export const PLATFORM_SOURCE_STATUS_TONE = {
  off: "neutral",
  connected: "low",
  error: "critical",
  not_configured: "neutral",
} as const satisfies Record<CtemPlatformSourceStatus, "low" | "critical" | "neutral">;

/** Each reason in words, its parameters in braces. @i18n-keys */
export const PLATFORM_SOURCE_REASON = {
  no_run_as: "No run-as user. Turn the source on again and pick the user it provisions as.",
  run_as_inactive: "The run-as user is deactivated or no longer exists. Pick another run-as user.",
  run_as_no_integration: "The run-as user has no connected {provider} integration. Connect {provider} for that user, or pick a user who has it.",
  no_webhook_secret: "The integration has no webhook secret. Set one on the integration so XTM One can authenticate its deliveries.",
  no_token: "No token could be read for the integration as the run-as user.",
  integration_not_found: "The integration no longer exists.",
  organization_context_missing: "Fill {fields} in Settings > Organization: the source is not provisioned without them.",
  objects_not_removed: "The objects could not be removed from the source platform. The next check tries again: {detail}",
  not_provisioned_yet: "Not provisioned yet. The next check provisions it.",
  provider_error: "The source platform refused: {detail}",
} as const satisfies Record<CtemPlatformSourceReasonCode, string>;

/**
 * What fixes each reason, in the reader's language (UX round 1 on #2653, B2): mirrors the backend's
 * `ctem_platform_sources.FIX_ACTIONS`, whose English `fix` the page never renders. @i18n-keys
 */
export const PLATFORM_SOURCE_FIX = {
  no_run_as: "Turn the source on again and pick the user it provisions as.",
  run_as_inactive: "Pick another run-as user.",
  run_as_no_integration: "Connect the integration for the run-as user, or pick a user who has it.",
  no_webhook_secret: "Set a webhook secret on the integration.",
  no_token: "Authorize the integration again as the run-as user.",
  integration_not_found: "Turn the source on again on an existing integration.",
  organization_context_missing: "Fill the missing fields in Settings > Organization.",
  objects_not_removed: "The next check removes them again; remove them by hand in the source platform if it keeps failing.",
  not_provisioned_yet: "Wait for the next check, or turn the source off and on again.",
  provider_error: "Check the integration's connection and the run-as user's rights in the source platform.",
} as const satisfies Record<CtemPlatformSourceReasonCode, string>;

/** The page a fix opens when it is not this page's own switch (the routes of `FIX_ACTIONS`). */
const SIGNALS_PAGE = "/ctem/signals";

type Translate = (key: string, values?: Record<string, string | number>) => string;

/** A sentence ending with its full stop, so the next one never runs into it (B2 of #2653). */
function sentence(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return "";
  return /[.!?。]$/.test(trimmed) ? trimmed : `${trimmed}.`;
}

/** The fix of a reason in the reader's language; `null` when there is no reason. */
function platformSourceFix(source: { reason_code?: CtemPlatformSourceReasonCode | null }, t_i18n: Translate): string | null {
  if (!source.reason_code) return null;
  return t_i18n(PLATFORM_SOURCE_FIX[source.reason_code] ?? PLATFORM_SOURCE_FIX.provider_error);
}

/** Where the fix is done, when it is another page than the signal sources page itself (its switch). */
export function platformSourceFixRoute(source: { fix_route?: string | null }): string | null {
  const route = source.fix_route ?? "";
  return route.startsWith("/") && route !== SIGNALS_PAGE ? route : null;
}

/** A reason in the reader's language; `null` when there is none. */
export function platformSourceReason(
  source: Pick<CtemPlatformSource, "reason_code" | "reason_params">,
  t_i18n: Translate,
): string | null {
  if (!source.reason_code) return null;
  return t_i18n(PLATFORM_SOURCE_REASON[source.reason_code] ?? PLATFORM_SOURCE_REASON.provider_error, {
    provider: "",
    fields: "",
    detail: "",
    ...(source.reason_params ?? {}),
  });
}

/**
 * One line of a source's reason on one integration (UX round 1 on #2653, B1 and B2): the integration's name
 * when known, the reason ended by its full stop, its fix in the reader's language and, when given, the last attempt.
 */
export function sourceReasonLine(
  entry: Pick<CtemPlatformSource, "reason_code" | "reason_params">,
  integration: string | null | undefined,
  t_i18n: Translate,
  when?: string | null,
): string {
  const reason = sentence(platformSourceReason(entry, t_i18n) ?? "");
  if (!reason) return "";
  const fix = sentence(platformSourceFix(entry, t_i18n) ?? "");
  const text = [reason, fix && fix !== reason && !reason.includes(fix) ? fix : "", when ?? ""].filter(Boolean).join(" ");
  return integration ? t_i18n("{integration}: {text}", { integration, text }) : text;
}

/**
 * The lines of a source in error on a path (#2637): one per integration, named (B1 of #2653), with its reason,
 * its fix and when it was last tried; none when the path answer carries no error. Read from the path answer,
 * so a reader who cannot list the platform sources still reads why (without the integration's name).
 */
export function sourceErrorLines(
  source: Pick<CtemSignalPathSource, "errors">,
  names: ReadonlyMap<string, string>,
  t_i18n: Translate,
  formatTime: (iso: string) => string,
): string[] {
  return (source.errors ?? [])
    .map((e) => {
      const when = e.last_attempt_at ? t_i18n("Last attempt {time}.", { time: formatTime(e.last_attempt_at) }) : "";
      return sourceReasonLine(e, names.get(e.integration_id), t_i18n, when);
    })
    .filter(Boolean);
}

/**
 * Where each provisioned object opens in its platform (#2583 point 2), from the integration's web UI host.
 * A playbook opens by its id; a notifier and a trigger have no page of their own there, so they open the
 * list that holds them, where the object is found by its "XTM One - ..." name.
 */
const OBJECT_ROUTES: Record<string, Partial<Record<CtemPlatformSourceObject["kind"], (id: string) => string>>> = {
  opencti: {
    notifier: () => "/dashboard/settings/customization/notifiers",
    trigger: () => "/dashboard/profile/triggers",
    playbook: (id) => `/dashboard/data/processing/automation/${encodeURIComponent(id)}`,
  },
  openaev: {
    notifier: () => "/admin/settings/customization/notifiers",
    trigger: () => "/admin/profile/triggers",
  },
};

/** The Open in label of each object kind. @i18n-keys */
export const PLATFORM_OBJECT_OPEN_LABEL = {
  notifier: "Open the notifier in {platform}",
  trigger: "Open the triggers in {platform}",
  playbook: "Open the playbook in {platform}",
} as const satisfies Record<CtemPlatformSourceObject["kind"], string>;

/** The visible text of each Open in button, the object kind it opens (UX advisory A1 of #2592). @i18n-keys */
export const PLATFORM_OBJECT_KIND_TEXT = {
  notifier: "Notifier",
  trigger: "Triggers",
  playbook: "Playbook",
} as const satisfies Record<CtemPlatformSourceObject["kind"], string>;

/** One Open in link: the object, its kind, and the URL in its platform. */
export interface PlatformObjectLink {
  kind: CtemPlatformSourceObject["kind"];
  id: string;
  href: string;
  /** The label of the path a per-path object serves (#2627); none for an object of the whole source. */
  pathLabel?: string;
}

/** The Open in links of a source's objects; none without a web UI host or a route for the kind. */
export function platformObjectLinks(
  source: Pick<CtemPlatformSource, "provider" | "objects" | "integration_url">,
): PlatformObjectLink[] {
  const base = (source.integration_url ?? "").replace(/\/+$/, "");
  if (!/^https?:\/\//i.test(base)) return [];
  const routes = OBJECT_ROUTES[source.provider] ?? {};
  const links: PlatformObjectLink[] = [];
  for (const object of source.objects ?? []) {
    const route = routes[object.kind];
    if (route) links.push({ kind: object.kind, id: object.id, href: `${base}${route(object.id)}`, pathLabel: object.path_label });
  }
  return links;
}

/**
 * The paths a source's one switch turns off when it feeds more than one (#2583 point 6): none for a source
 * that feeds a single path, whose own row already names it.
 */
export function sharedFeeds(source: Pick<CtemPlatformSource, "feeds">): { path: string; label: string }[] {
  const feeds = source.feeds ?? [];
  return feeds.length > 1 ? feeds : [];
}

/**
 * How many legacy objects the "Remove legacy OpenCTI objects" action would clear under these sources
 * (#2583 point 4, #2627): on the OpenCTI live listener only (one place on the page), the objects XTM One
 * still holds on the integrations a Signals source is on for; 0 hides the action.
 */
export function legacyCleanupCount(
  source: Pick<CtemSignalPathSource, "provider" | "kind">,
  own: readonly Pick<CtemPlatformSource, "enabled" | "legacy_pending">[],
): number {
  if (source.provider !== "opencti" || source.kind !== "listener") return 0;
  return own.filter((s) => s.enabled).reduce((n, s) => n + (s.legacy_pending ?? 0), 0);
}

/** The refusal of an enable (`detail: {code, params}`) in the reader's language, or `null` for any other error. */
export function enableRefusalText(error: unknown, t_i18n: Translate): string | null {
  const detail = (error as { detail?: unknown } | null)?.detail;
  if (!detail || typeof detail !== "object") return null;
  const { code, params } = detail as { code?: unknown; params?: unknown };
  if (typeof code !== "string" || !(code in PLATFORM_SOURCE_REASON)) return null;
  return platformSourceReason(
    { reason_code: code as CtemPlatformSourceReasonCode, reason_params: (params && typeof params === "object" ? params : {}) as Record<string, string> },
    t_i18n,
  );
}
