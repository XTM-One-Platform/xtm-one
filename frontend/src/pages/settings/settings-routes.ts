// SPDX-License-Identifier: Apache-2.0

/**
 * The Settings sections and the redirect table of the old tabs (#9173, S1).
 *
 * Settings has 7 sections, each with its sub-sections in the URL:
 * `/settings/<section>/<sub-section>`. Every route of the 12 old tabs
 * redirects here, keeping its query and hash (an old OAuth callback with
 * `?gh_connected=true`, an old hub link with `#xtm-hub`).
 * `settings-routes.test.ts` fails on an old route literal anywhere else in
 * the frontend or the backend.
 */

export type SettingsTab = "organization" | "automations" | "integrations" | "ai" | "security" | "usage" | "platform";

export const SETTINGS_TABS: readonly SettingsTab[] = [
  "organization", "automations", "integrations", "ai", "security", "usage", "platform",
];

/** The first section: bare `/settings` opens it. */
export const DEFAULT_SETTINGS_TAB: SettingsTab = "organization";

export type OrganizationSubsection = "profile" | "departments" | "units" | "rates";
export type AutomationsSection = "fleets" | "ctem-agents" | "agent-runs" | "quality" | "health";
export type IntegrationsSettingsSection = "catalog" | "oauth" | "channels" | "mcp";
export type AiSection = "models" | "media" | "assistant" | "knowledge";
export type UsageSection = "quotas" | "observability" | "pricing";

/** The Platform Health settings: the Platform Health page links here (#9301 N4, renamed #101 S1). */
export const HEALTH_SETTINGS_PATH = "/settings/automations/health";

/** The old tab segment and where it lives now. */
export const LEGACY_SETTINGS_ROUTES: Readonly<Record<string, string>> = {
  parameters: "/settings/platform",
  models: "/settings/ai/models",
  assistant: "/settings/ai/assistant",
  channels: "/settings/integrations/channels",
  mcp: "/settings/integrations/mcp",
  quotas: "/settings/usage/quotas",
  observability: "/settings/usage/observability",
  quality: "/settings/automations/quality",
  concierge: HEALTH_SETTINGS_PATH,
};

/**
 * Where an old Settings route goes, its query and hash kept; `null` when the
 * tab segment is not an old one.
 */
export function legacySettingsTarget(tab: string | undefined, search: string, hash: string): string | null {
  if (!tab || !Object.prototype.hasOwnProperty.call(LEGACY_SETTINGS_ROUTES, tab)) return null;
  return `${LEGACY_SETTINGS_ROUTES[tab]}${search}${hash}`;
}

/**
 * The sub-section bare `/settings/automations` opens (UX C3): Fleets in
 * XTM One mode, Agent runs in Copilot mode. An XTM One only section in Copilot
 * mode falls back to this default; Platform Health is built in since #101 (S1),
 * and the former `concierge` segment opens it.
 * Fleets is in every mode (#9162 scope: fleet member configuration and its
 * presets are generic fleet features); only CTEM stage agents is XTM One only.
 */
export function automationsSection(
  requested: string | undefined,
  { isXtmOne }: { isXtmOne: boolean },
): AutomationsSection {
  const fallback: AutomationsSection = isXtmOne ? "fleets" : "agent-runs";
  switch (requested) {
    case "ctem-agents":
      return isXtmOne ? requested : fallback;
    case "fleets":
    case "agent-runs":
    case "quality":
    case "health":
      return requested;
    case "concierge":
      return "health";
    default:
      return fallback;
  }
}

/** A sub-section among the allowed ones, else the first. */
export function pickSection<T extends string>(requested: string | undefined, allowed: readonly T[]): T {
  return (allowed as readonly string[]).includes(requested ?? "") ? (requested as T) : allowed[0];
}

/** The path of a sub-section: the first one is the bare section path. */
export function settingsSectionPath(tab: SettingsTab, section: string, first: string): string {
  return section === first ? `/settings/${tab}` : `/settings/${tab}/${section}`;
}
