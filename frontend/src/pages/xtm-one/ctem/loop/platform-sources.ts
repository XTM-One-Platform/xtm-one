// SPDX-License-Identifier: Apache-2.0

/**
 * The platform signal sources' rules (#2434, dev-docs/ctem-signals.md section 4bis): which
 * sources XTM One provisions in their platform, their status tone, and their reason in words.
 * Pure, so the mapping is unit-tested.
 */
import type {
  CtemPlatformSource,
  CtemPlatformSourceReasonCode,
  CtemPlatformSourceStatus,
  CtemSignalPathSource,
} from "@/hooks/api/useCtemSignalsApi";

/** The `provider:kind` sources XTM One provisions on enable (`ctem_platform_sources.PROVISIONED`). */
export const PROVISIONED_SOURCES: ReadonlySet<string> = new Set(["openaev:listener", "opencti:listener", "opencti:pir_playbook"]);

/** Whether a source of a path is switched on, with a run-as, as a platform source. */
export function isProvisioned(source: Pick<CtemSignalPathSource, "provider" | "kind">): boolean {
  return PROVISIONED_SOURCES.has(`${source.provider}:${source.kind}`);
}

/** The platform sources of one path source: one per integration of its provider. */
export function platformSourcesOf(
  source: Pick<CtemSignalPathSource, "provider" | "kind">,
  all: readonly CtemPlatformSource[],
): CtemPlatformSource[] {
  return all.filter((s) => s.provider === source.provider && s.kind === source.kind);
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
