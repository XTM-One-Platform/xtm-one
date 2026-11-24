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
 * Pure helpers of the About page's prerequisites (#10258 S2): the checklist
 * derived from the fleet's configuration (`live_prerequisites`), its count
 * for the status band, its reading order, the translated label and reason of
 * a line and the "Fix with AI" request. Nothing here calls the API.
 */
import type { FleetLivePrerequisite } from "@/hooks/api/useFleetApi";

type Translate = (id: string, values?: Record<string, string | number>) => string;

/**
 * Why a line is not met, by the reason code the backend sends
 * (`fleet_about.REASON_CODES`; a backend test keeps both lists equal).
 * `{date}` is the expiry, formatted for the reader's locale. @i18n-keys
 */
export const PREREQUISITE_REASONS = {
  integration_deleted: "The integration was deleted",
  integration_disconnected: "Disconnected: reconnect it",
  integration_not_signed_in: "Not signed in yet: connect it",
  integration_error: "In error: reconnect it",
  integration_not_connected: "Not connected: reconnect it",
  token_expired: "Token expired on {date}",
  own_account_missing: "Connect your own account",
  account_switched_off: "Switched off",
  account_integration_deleted: "Its integration was deleted",
  account_paused: "Paused",
  account_error: "In error",
  account_setup: "Setup not finished",
  mcp_switched_off: "Switched off",
  mcp_sign_in: "Sign-in needed",
  not_shared: "Not shared with you",
  another_person_account: "Another person's account",
  target_deleted: "The target agent was deleted",
  target_switched_off: "The target agent is switched off",
  deleted: "It was deleted",
  automations_off: "Every automation is off",
  platform_health_off: "Platform Health is off in Settings",
  not_filled: "Not filled",
} as const;

/**
 * The labels the platform writes (`label_key`), with their `{values}`
 * (`fleet_about.LABEL_*` and `PROFILE_LABELS`; a backend test keeps both
 * lists equal). A name a person gave stays as written. @i18n-keys
 */
export const PREREQUISITE_LABELS = [
  "{provider} (your own account)",
  "Handover to {name}",
  "Handover to an agent",
  "At least one automation switched on",
  "Platform Health switched on in Settings",
  "Integration",
  "Knowledge base",
  "MCP server",
  "Sector in the organization profile",
  "Regions in the organization profile",
  "Primary assets in the organization profile",
  "Technology in the organization profile",
  "AI systems listed in the organization profile",
  "A tracked organization with monitoring on",
] as const;

const KNOWN_LABELS: ReadonlySet<string> = new Set(PREREQUISITE_LABELS);

/** The line's label in the reader's language: a platform label translated, a name as written. */
export function prerequisiteLabel(line: FleetLivePrerequisite, t: Translate): string {
  if (line.label_key && KNOWN_LABELS.has(line.label_key)) return t(line.label_key, line.label_params ?? {});
  return line.label;
}

/**
 * The line's reason in the reader's language, never a raw status: the code
 * translated, then the detail an account reported (its own words). A code
 * this build does not know shows nothing rather than the code.
 */
export function prerequisiteReason(line: FleetLivePrerequisite, t: Translate, formatDate: (iso: string) => string): string {
  const code = line.reason_code as keyof typeof PREREQUISITE_REASONS | undefined;
  if (!code || !(code in PREREQUISITE_REASONS)) return "";
  const params = line.reason_params ?? {};
  const text = t(PREREQUISITE_REASONS[code], params.date ? { date: formatDate(params.date) } : {});
  return params.detail ? `${text}: ${params.detail}` : text;
}

/** Reading order: what needs an action first, then what the reader may not see, then what a person confirms. */
const ORDER: Record<string, number> = { not_met: 0, unreadable: 1, to_confirm: 2 };

/**
 * The lines that need a look (not met, not readable, to confirm) in reading
 * order, and the met lines apart (the page folds them into "N met"). A manual
 * line a person confirmed (#10469) is met and folds with them; one to confirm
 * sits with the open lines, with its Confirm action, never in a block apart.
 */
export function prerequisiteGroups(lines: readonly FleetLivePrerequisite[]): {
  open: FleetLivePrerequisite[];
  met: FleetLivePrerequisite[];
} {
  const open = lines
    .filter((l) => l.state !== "met")
    .map((line, i) => ({ line, i }))
    .sort((a, b) => (ORDER[a.line.state] ?? 3) - (ORDER[b.line.state] ?? 3) || a.i - b.i)
    .map(({ line }) => line);
  return { open, met: lines.filter((l) => l.state === "met") };
}

/**
 * The status band's count: lines met over every line, a confirmed manual line
 * counted as met (#10469). `failing` are the lines not met, `fixable` those of
 * them the reader may open the fix of (the ones Fix with AI works through),
 * and `toConfirm` the manual lines a fleet manager has still to confirm.
 */
export function prerequisiteSummary(lines: readonly FleetLivePrerequisite[]): {
  met: number;
  total: number;
  failing: FleetLivePrerequisite[];
  fixable: FleetLivePrerequisite[];
  toConfirm: FleetLivePrerequisite[];
} {
  const failing = lines.filter((l) => l.state === "not_met");
  return {
    met: lines.filter((l) => l.state === "met").length,
    total: lines.length,
    failing,
    fixable: failing.filter((l) => l.can_fix && l.fix_route),
    toConfirm: lines.filter((l) => l.state === "to_confirm"),
  };
}

/** The failing lines as the Fix with AI request lists them: one line each, with its reason and its check. */
export function failingLinesText(
  failing: readonly FleetLivePrerequisite[],
  t: Translate,
  formatDate: (iso: string) => string,
): string {
  return failing
    .map((l) => {
      const reason = prerequisiteReason(l, t, formatDate);
      return `- ${prerequisiteLabel(l, t)}${reason ? `: ${reason}` : ""} (${l.check})`;
    })
    .join("\n");
}
