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
} as const satisfies Record<CtemPlatformSourceStatus, string>;

/** Connected reads healthy, an error reads critical, off is neutral. */
export const PLATFORM_SOURCE_STATUS_TONE = {
  off: "neutral",
  connected: "low",
  error: "critical",
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

type Translate = (key: string, values?: Record<string, string | number>) => string;

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

/** One Open in link: the object, its kind, and the URL in its platform. */
export interface PlatformObjectLink {
  kind: CtemPlatformSourceObject["kind"];
  id: string;
  href: string;
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
    if (route) links.push({ kind: object.kind, id: object.id, href: `${base}${route(object.id)}` });
  }
  return links;
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
