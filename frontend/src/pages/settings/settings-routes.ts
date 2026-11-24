// SPDX-License-Identifier: Apache-2.0

/**
 * The Settings sections (#9173, S1).
 *
 * Settings has 7 sections, each with its sub-sections in the URL:
 * `/settings/<section>/<sub-section>`. A segment that is not a section is not
 * found: the former tab routes are not redirected (#797).
 * `settings-routes.test.ts` fails on a former route literal anywhere in the
 * frontend or the backend.
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
export type UsageSection = "quotas" | "observability" | "pricing" | "storage";

/** The Platform Health settings: the Platform Health page links here (#9301 N4, renamed #101 S1). */
export const HEALTH_SETTINGS_PATH = "/settings/automations/health";

/** The former tab segments, retired with no redirect (#797); the test keeps them out of the code. */
export const RETIRED_SETTINGS_SEGMENTS: readonly string[] = [
  "parameters", "models", "assistant", "channels", "mcp", "quotas", "observability", "quality", "concierge",
];

/** Whether a `/settings/:tab` segment is a section; bare `/settings` is (the first section). */
export function isSettingsTab(tab: string | undefined): boolean {
  return tab === undefined || (SETTINGS_TABS as readonly string[]).includes(tab);
}

/**
 * The sub-section bare `/settings/automations` opens (UX C3): Fleets in
 * XTM One mode, Agent runs in Copilot mode. An XTM One only section in Copilot
 * mode falls back to this default; Platform Health is built in since #101 (S1).
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
