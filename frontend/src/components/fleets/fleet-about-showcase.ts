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
 * Pure helpers of the fleet About showcase (#9539 slice S2): the fleet's true
 * status, its key figures and the section index, computed from what the page
 * already reads (documentation, health). Nothing here calls the API.
 */
import type { FleetDocModule, FleetDocStructured, FleetHealth, FleetLivePrerequisite, FleetMemberHealth } from "@/hooks/api/useFleetApi";
import type { FleetOptIn } from "@/lib/fleet-activity";
import type { Assignment } from "@/types";

import { prerequisiteSummary } from "./fleet-prerequisites";

/**
 * The fleet's status as the hero says it: never "Healthy". The opt-in states
 * of the header pill, rendered by the one `FleetOptInBadge` (UX round 1 on
 * #9835, B2): the labels and variants live in that component only.
 */
type FleetAboutStatus = FleetOptIn;

/** What the status reads of a fleet's health: its members and their latest run. */
type StatusHealth = { members: readonly Pick<FleetHealth["members"][number], "agent_id" | "last_run_at">[] };

/** The fleet's automations: the assignments of its members, and how many are switched on. */
interface AutomationCount {
  on: number;
  total: number;
}

/**
 * Count the automations of the fleet's members (UX B1 of #9546): an automation
 * is an assignment, and only a switched-on one starts runs. The agent's own
 * enabled flag says nothing about it. `null` while either read is unknown.
 */
export function automationCount(
  health: StatusHealth | null | undefined,
  assignments: readonly Pick<Assignment, "agent_id" | "enabled">[] | null | undefined,
): AutomationCount | null {
  if (!health || !assignments) return null;
  const members = new Set(health.members.map((m) => m.agent_id));
  const own = assignments.filter((a) => members.has(a.agent_id));
  return { on: own.filter((a) => a.enabled).length, total: own.length };
}

/**
 * The automations the About tile shows (UX round 1 B2 on #1092): the health's
 * own count, the fleet automations the header pill, the Fleets list and the
 * CTEM loop setup read (`fleet_views.fleet_automation_counts`), so one fleet
 * shows one figure everywhere; the members' assignments only while the health
 * carries no count.
 */
export function shownAutomationCount(
  health: (StatusHealth & { automations?: { enabled: number; total: number } }) | null | undefined,
  assignments: readonly Pick<Assignment, "agent_id" | "enabled">[] | null | undefined,
): AutomationCount | null {
  if (health?.automations) return { on: health.automations.enabled, total: health.automations.total };
  return automationCount(health, assignments);
}

/**
 * The status from the fleet's automations: none, or none on and none ever ran,
 * is Not set up; none on after a run is Paused; some on is Partially on; every
 * one on is On. `null` while the count is unknown (still loading, refused).
 */
export function aboutStatus(
  health: StatusHealth | null | undefined,
  automations: AutomationCount | null,
): FleetAboutStatus | null {
  if (!health || !automations) return null;
  if (automations.on === 0) return automations.total > 0 && lastRunAt(health) ? "paused" : "not_set_up";
  return automations.on < automations.total ? "partial" : "on";
}

/** The latest run of any member, ISO 8601, or `null` when none ran. */
export function lastRunAt(health: StatusHealth | null | undefined): string | null {
  let latest: string | null = null;
  for (const m of health?.members ?? []) {
    if (m.last_run_at && (!latest || m.last_run_at > latest)) latest = m.last_run_at;
  }
  return latest;
}

/**
 * The monthly run range summed over the quota lines, or `null` without any.
 * `openEnded` (#10108): one event-driven line has no fixed top, so the range is a floor.
 */
export function quotaRange(
  structured: FleetDocStructured | null | undefined,
): { min: number; max: number; openEnded: boolean; kinds: QuotaKind[] } | null {
  const lines = structured?.quota ?? [];
  if (lines.length === 0) return null;
  const range = lines.reduce<{ min: number; max: number; openEnded: boolean; kinds: Set<QuotaKind> }>(
    (acc, q) => {
      for (const k of q.kinds ?? []) acc.kinds.add(k);
      return { min: acc.min + q.runs_min, max: acc.max + q.runs_max, openEnded: acc.openEnded || !!q.open_ended, kinds: acc.kinds };
    },
    { min: 0, max: 0, openEnded: false, kinds: new Set<QuotaKind>() },
  );
  return { ...range, kinds: QUOTA_KINDS.filter((k) => range.kinds.has(k)) };
}

export type QuotaKind = "schedule" | "event" | "manual";
const QUOTA_KINDS: readonly QuotaKind[] = ["schedule", "event", "manual"];

/**
 * Which subtitle the hero quota card shows (UX r1 B2 on #10112): built from the trigger kinds present,
 * never "schedules plus event-driven" on a fleet with neither. `legacy` for a version written before `kinds`.
 */
export function quotaSubtitle(range: {
  openEnded: boolean;
  kinds: readonly QuotaKind[];
}): "fixed" | "schedule_event" | "event" | "event_manual" | "manual" | "schedule_manual" | "legacy" {
  if (!range.openEnded) return "fixed";
  const has = (k: QuotaKind) => range.kinds.includes(k);
  if (range.kinds.length === 0) return "legacy";
  if (has("event")) return has("schedule") ? "schedule_event" : has("manual") ? "event_manual" : "event";
  return has("schedule") ? "schedule_manual" : "manual";
}

/**
 * Which basis a quota row shows (UX r1 B3 on #10112), rendered in the reader's language: `observed` runs,
 * the `schedule` tick, `none` (no fixed estimate), `grouped` for a group of several kinds, `written` for the
 * basis text itself (an AI estimate, or a version written before `kinds`), `null` for no basis.
 */
export function quotaBasisKind(q: {
  basis?: string;
  runs_max: number;
  kinds?: readonly QuotaKind[];
  observed?: boolean;
}): "observed" | "schedule" | "none" | "grouped" | "written" | null {
  const kinds = q.kinds ?? [];
  if (kinds.length === 0) return q.basis ? "written" : null;
  if (q.observed) return "observed";
  if (kinds.length > 1) return "grouped";
  if (kinds[0] === "schedule") return "schedule";
  if (q.runs_max > 0) return q.basis ? "written" : null;
  return "none";
}

/**
 * What the right side of a quota row says (UX r1 B1 on #10112): a floor when runs were seen or estimated,
 * "Started by hand only" for hand-started triggers, "Event-driven" otherwise, a range or one figure for a schedule.
 */
export function quotaValue(q: { runs_min: number; runs_max: number; open_ended?: boolean; kinds?: readonly QuotaKind[] }): "floor" | "manual" | "event" | "exact" | "range" {
  if (q.open_ended) {
    if (q.runs_max > 0) return "floor";
    const kinds = q.kinds ?? [];
    return kinds.length > 0 && kinds.every((k) => k === "manual") ? "manual" : "event";
  }
  return q.runs_min === q.runs_max ? "exact" : "range";
}

/** The catalog label of each base module (`fleet_docs.BASE_MODULES`). @i18n-keys */
const MODULE_LABELS = {
  opencti: "OpenCTI",
  openaev: "OpenAEV",
  opencrq: "OpenCRQ",
  schedule: "Schedule",
  work_item: "Work item",
  platform: "XTM One",
  webhook: "Webhook",
  manual: "Manual",
} as const;

/** The base modules that draw an icon, not a provider logo. */
export const ICON_MODULES: ReadonlySet<string> = new Set(["schedule", "work_item", "platform", "webhook", "manual"]);

/**
 * How a module reads (#9979): a base module's catalog label (to translate),
 * else the integration provider's display name (a brand, never translated),
 * else the key humanized. Never remapped to the platform.
 */
export function moduleLabel(
  module: FleetDocModule | null | undefined,
  providerNames: Readonly<Record<string, string>>,
): { text: string; translate: boolean } {
  const key = (module || "platform").toLowerCase();
  if (key in MODULE_LABELS) return { text: MODULE_LABELS[key as keyof typeof MODULE_LABELS], translate: true };
  const brand = providerNames[key];
  if (brand) return { text: brand, translate: false };
  const words = key.split(/[_-]+/).filter(Boolean).join(" ");
  return { text: words.charAt(0).toUpperCase() + words.slice(1), translate: false };
}

/** The trigger kinds `fleet_doc_generation.TRIGGER_WORDS` writes. @i18n-keys */
const TRIGGER_KINDS = {
  Schedule: "Schedule",
  Event: "Event",
  "Platform event": "Platform event",
  "Work item": "Work item",
  Webhook: "Webhook",
  Manual: "Manual",
} as const;

/** A trigger kind in the reader's language; an unknown kind reads as written. */
export function triggerKindLabel(kind: string | undefined, t: (key: string) => string): string {
  if (!kind) return "";
  return kind in TRIGGER_KINDS ? t(TRIGGER_KINDS[kind as keyof typeof TRIGGER_KINDS]) : kind;
}

/**
 * Whether an automation of the documentation is on (UX r2 B1 on #9988). The server stores the
 * structured parts without their defaults, so an automation switched on carries no `enabled`
 * key: only an explicit `false` is off.
 */
export function automationOn(automation: { enabled?: boolean | null } | null | undefined): boolean {
  return automation?.enabled !== false;
}

/** At most this many provider chips or tool badges per Integrations and tools card, the rest as "+N" (UX r2 B3 on #9988). */
export const INTEGRATION_CARD_CHIPS_MAX = 6;

/**
 * At most this many provider chips (logo and name) on the one-line providers row of an Integrations and tools card,
 * the "+N more" chip included (UX r3 B1 on #9988): three named providers and the count fit a 384 px card in one line.
 */
export const INTEGRATION_CARD_PROVIDERS_MAX = 4;

/** *items* cut to *max* for one same-size card: the items shown and how many are left for the "+N" chip, never a silent cut. */
export function clampChips<T>(items: readonly T[] | null | undefined, max: number = INTEGRATION_CARD_CHIPS_MAX): { shown: T[]; rest: number } {
  const all = items ?? [];
  if (all.length <= max) return { shown: [...all], rest: 0 };
  // The "+N" chip takes one slot, so the card never grows past *max* chips.
  const shown = all.slice(0, Math.max(0, max - 1));
  return { shown, rest: all.length - shown.length };
}

/** The members that use an integration or a tool, each with what it uses; a member using nothing is left out. */
export function integrationUses(structured: FleetDocStructured | null | undefined): NonNullable<FleetDocStructured["integrations"]> {
  return (structured?.integrations ?? []).filter(
    (u) => (u.integrations?.length ?? 0) + (u.mcp_servers?.length ?? 0) + (u.tool_groups?.length ?? 0) + (u.skills?.length ?? 0) > 0,
  );
}

/**
 * The quota tile's figure (#10123): "about N" when the estimate is one number (low equals high), a floor
 * for an open-ended estimate, else a range; `null` without any quota line. An open-ended estimate with no
 * fixed run at all (UX r2 B2 on #10147) is the named state instead of "0 or more": `event` when an event
 * starts it, `manual` when it is only started by hand.
 */
export function quotaEstimate(
  range: { min: number; max: number; openEnded: boolean; kinds?: readonly QuotaKind[] } | null,
):
  | { kind: "about"; value: number }
  | { kind: "floor"; value: number }
  | { kind: "range"; min: number; max: number }
  | { kind: "named"; state: "event" | "manual" }
  | null {
  if (!range) return null;
  if (range.openEnded && range.max === 0) {
    const kinds = range.kinds ?? [];
    return { kind: "named", state: kinds.length > 0 && !kinds.includes("event") && kinds.includes("manual") ? "manual" : "event" };
  }
  if (range.openEnded) return { kind: "floor", value: range.max };
  if (range.min === range.max) return { kind: "about", value: range.max };
  return { kind: "range", min: range.min, max: range.max };
}

/**
 * The prerequisites checklist order (UX r2 B1 on #10147): the unmet first, then the ones to check by hand,
 * then the met, which the page folds into one "N met" row. Stable within each group.
 */
export function checklistGroups<T extends { met?: boolean | null }>(states: readonly T[]): { open: T[]; met: T[] } {
  const unmet = states.filter((p) => p.met === false);
  const unknown = states.filter((p) => p.met !== false && p.met !== true);
  return { open: [...unmet, ...unknown], met: states.filter((p) => p.met === true) };
}

/**
 * The prerequisites figure of the compact setup row (UX r7 B1 on #10147): the live checklist (#10258) when it has
 * lines, counted as the hero chip counts it (`prerequisiteSummary`), so the row and the chip never disagree; the
 * documented states only as a fallback. `null` when neither has a line.
 */
export function setupPrerequisites(
  live: readonly FleetLivePrerequisite[] | null | undefined,
  states: readonly { met?: boolean | null }[] | null | undefined,
): { met: number; total: number } | null {
  if (live && live.length > 0) {
    const { met, total } = prerequisiteSummary(live);
    return total > 0 ? { met, total } : null;
  }
  if (states && states.length > 0) return { met: states.filter((p) => p.met === true).length, total: states.length };
  return null;
}

/** The success rate of the 30-day runs, a whole percent, or `null` without a finished run. */
export function successRate(stats: { succeeded: number; failed: number } | null | undefined): number | null {
  const finished = (stats?.succeeded ?? 0) + (stats?.failed ?? 0);
  if (!stats || finished === 0) return null;
  return Math.round((stats.succeeded / finished) * 100);
}

/** One member card of the About page (#10123): the live health row joined with what the documentation says. */
export interface AboutMemberCard {
  agentId: string;
  name: string;
  /** The one-line role the documentation writes, free text (not a role key), or empty. */
  roleLine: string;
  orchestrator: boolean;
  status: FleetMemberHealth["status"];
  lastRunAt: string | null;
  staleItems: number;
  /** Its automations switched on, of its total. */
  automationsOn: number;
  automationsTotal: number;
  /** Integration provider keys it uses, as the documentation lists them. */
  providers: string[];
}

/**
 * The member cards in reading order: the orchestrator first, then by name. Every figure is live (health,
 * assignments); the role and the providers come from the documentation, matched by the member's name.
 */
export function memberCards(
  health: { members: readonly FleetMemberHealth[] } | null | undefined,
  structured: FleetDocStructured | null | undefined,
  assignments: readonly Pick<Assignment, "agent_id" | "enabled">[] | null | undefined,
  orchestratorId: string | null | undefined,
): AboutMemberCard[] {
  const roles = new Map((structured?.members ?? []).map((m) => [m.name, m.role ?? ""]));
  const uses = new Map((structured?.integrations ?? []).map((u) => [u.member, u.integrations ?? []]));
  const cards = (health?.members ?? []).map((m) => {
    const own = (assignments ?? []).filter((a) => a.agent_id === m.agent_id);
    return {
      agentId: m.agent_id,
      name: m.name,
      roleLine: roles.get(m.name) ?? "",
      orchestrator: m.agent_id === orchestratorId,
      status: m.status,
      lastRunAt: m.last_run_at,
      staleItems: m.stale_work_items,
      automationsOn: own.filter((a) => a.enabled).length,
      automationsTotal: own.length,
      providers: uses.get(m.name) ?? [],
    };
  });
  return cards.sort((a, b) => Number(b.orchestrator) - Number(a.orchestrator) || a.name.localeCompare(b.name));
}

/** The trigger kinds of the fleet's automations, each once, in a stable order: the chips of "How it works". */
export function triggerKinds(structured: FleetDocStructured | null | undefined): string[] {
  const seen = new Set<string>();
  for (const a of structured?.automations ?? []) if (a.trigger) seen.add(a.trigger);
  return [...seen].sort();
}
