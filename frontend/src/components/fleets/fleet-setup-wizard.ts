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
 * One guided setup to enable any fleet (#1151): the step order, their copy
 * and the pure rules `FleetSetupWizard` follows. It replaces the separate
 * Enable fleet dialog: the plan (`POST /fleets/{id}/enable` with `dry_run`)
 * and the one enable call are the same as before, reached through the steps.
 *
 * Resume is client-side, per fleet and per browser (the smallest safe change:
 * no endpoint, no migration). The CTEM loop fleet keeps its versioned
 * server-side setup (`GET /fleets/setup/ctem-loop`, #1087).
 */
import type { FleetEnableEstimate, FleetEnableItem } from "@/hooks/api/useFleetApi";

export type WizardStepKey = "about" | "integrations" | "autonomy" | "automations" | "review";

/** Every step in the order the wizard walks them. */
const WIZARD_STEPS: readonly WizardStepKey[] = ["about", "integrations", "autonomy", "automations", "review"];

/** @i18n-keys */
export const WIZARD_STEP_TITLES = {
  about: "What it does",
  integrations: "Integrations",
  autonomy: "Autonomy",
  automations: "Run as and automations",
  review: "Review and enable",
} as const satisfies Record<WizardStepKey, string>;

/** @i18n-keys */
export const WIZARD_STEP_INTROS = {
  about: "What the fleet does, what starts it and how many runs a month to expect. Nothing runs before the last step.",
  integrations: "Bind the tools the fleet's agents act through. A slot left empty only limits what they can do; you can complete the slots on the Members screen at any time.",
  autonomy: "Choose how much the fleet does without asking a person. You can change it, and each gate, later on the fleet page.",
  automations: "Who the automations run as, and which ones to switch on. An automation that needs a module this user has not connected stays off.",
  review: "What switches on when you confirm. Nothing is enabled before this step.",
} as const satisfies Record<WizardStepKey, string>;

/**
 * The steps a fleet walks: Integrations only when one of its members declares
 * a capability slot (`hasSlots` false drops it; unknown keeps it). With `from`
 * (a parent flow that already walked the earlier steps, the CTEM loop setup:
 * UX r1 B1), only the steps from it on: the earlier ones are not reachable.
 */
export function wizardSteps(hasSlots: boolean | null, from?: WizardStepKey): WizardStepKey[] {
  const walked = WIZARD_STEPS.filter((key) => key !== "integrations" || hasSlots !== false);
  const start = from ? walked.indexOf(from) : -1;
  return start > 0 ? walked.slice(start) : walked;
}

/** The step after *key* in *steps*, or null after the last one. */
export function nextWizardStep(steps: readonly WizardStepKey[], key: WizardStepKey): WizardStepKey | null {
  const index = steps.indexOf(key);
  return index >= 0 && index < steps.length - 1 ? steps[index + 1] : null;
}

/** The step before *key* in *steps*, or null on the first one. */
export function previousWizardStep(steps: readonly WizardStepKey[], key: WizardStepKey): WizardStepKey | null {
  const index = steps.indexOf(key);
  return index > 0 ? steps[index - 1] : null;
}

/** Where a person stopped in one fleet's setup, kept in the browser. */
export interface WizardProgress {
  step: WizardStepKey;
  /** The steps passed with Continue or Skip. */
  settled: WizardStepKey[];
  /** Integrations left for later ("I will do it later"). */
  skippedIntegrations: boolean;
}

const PROGRESS_PREFIX = "xtm.fleet-setup.";

function isStep(value: unknown): value is WizardStepKey {
  return typeof value === "string" && (WIZARD_STEPS as readonly string[]).includes(value);
}

/** The saved progress of a fleet's setup, or null (none, unreadable, storage off). */
export function readWizardProgress(fleetId: string, storage: Pick<Storage, "getItem"> | null = safeStorage()): WizardProgress | null {
  try {
    const raw = storage?.getItem(PROGRESS_PREFIX + fleetId);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<WizardProgress>;
    if (!isStep(parsed.step)) return null;
    return {
      step: parsed.step,
      settled: Array.isArray(parsed.settled) ? parsed.settled.filter(isStep) : [],
      skippedIntegrations: parsed.skippedIntegrations === true,
    };
  } catch {
    return null;
  }
}

/** Saves a fleet's progress; `null` clears it (the fleet was enabled). A storage refusal is ignored. */
export function writeWizardProgress(
  fleetId: string,
  progress: WizardProgress | null,
  storage: Pick<Storage, "setItem" | "removeItem"> | null = safeStorage(),
): void {
  try {
    if (progress) storage?.setItem(PROGRESS_PREFIX + fleetId, JSON.stringify(progress));
    else storage?.removeItem(PROGRESS_PREFIX + fleetId);
  } catch {
    // Private mode or a full quota: the setup still works, it only does not resume.
  }
}

function safeStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

/** The step the wizard opens on: the saved one when it is still walked, else the first. */
export function openingWizardStep(steps: readonly WizardStepKey[], saved: WizardProgress | null, start?: WizardStepKey): WizardStepKey {
  if (start && steps.includes(start)) return start;
  if (saved && steps.includes(saved.step)) return saved.step;
  return steps[0];
}

const PROVIDER_NAMES: Record<string, string> = { opencti: "OpenCTI", openaev: "OpenAEV", opencrq: "OpenCRQ" };

/** A provider's product name, the raw key for one the table does not know. */
export function providerName(provider: string): string {
  return PROVIDER_NAMES[provider] ?? provider;
}

/** The rows a pick keeps (every row but the ones left out) and the modules they miss, in the plan's order. */
export function pickedPlan(items: readonly FleetEnableItem[], left: ReadonlySet<string>) {
  const picked = items.filter((item) => !left.has(item.id));
  const seen = new Set(picked.flatMap((item) => item.missing));
  const order = items.flatMap((item) => item.missing).filter((p, i, all) => all.indexOf(p) === i);
  return {
    picked,
    missing: order.filter((p) => seen.has(p)),
    /** The ids to send: none when every row is picked (the server picks every one). */
    assignmentIds: picked.length === items.length ? undefined : picked.map((item) => item.id),
  };
}

/** Days a monthly load counts (the About tab basis). */
const MONTH_DAYS = 30;
/** Days a yearly load counts (a schedule rarer than once a month, UX round 2 B3 on #1550). */
const YEAR_DAYS = 365;

/**
 * The expected load of a pick (#1538): the runs a day its schedules start, how
 * many of its automations run once per event, and a cost a day from the mean
 * cost of one run the plan read (null without a run in its window). An order
 * of magnitude, never a quote.
 *
 * The per-item rates are summed unrounded and only the total is rounded, so a
 * pick of monthly schedules never reads as no run. Under one run a day the
 * load is said per month (`perMonth`): `runs` and `cost` then count a month
 * of `MONTH_DAYS` days, else a day (`runs` to one decimal). Under one run a
 * month once rounded (a quarterly or half-yearly schedule) it is said per year (`perYear`, `perMonth`
 * false): `runs` and `cost` then count `YEAR_DAYS` days.
 */
export function pickedLoad(picked: readonly FleetEnableItem[], estimate: FleetEnableEstimate | null | undefined) {
  const rawPerDay = picked.reduce((sum, item) => sum + (item.runs_per_day ?? 0), 0);
  // Rounded first: a monthly cron is 0.0333 a day, 0.999 a month, and stays said per month.
  const perYear = rawPerDay > 0 && Math.round(rawPerDay * MONTH_DAYS) < 1;
  const perMonth = !perYear && rawPerDay > 0 && rawPerDay < 1;
  const rawRuns = perYear ? rawPerDay * YEAR_DAYS : perMonth ? rawPerDay * MONTH_DAYS : rawPerDay;
  const runs = perYear || perMonth ? Math.max(1, Math.round(rawRuns)) : Math.round(rawRuns * 10) / 10;
  const eventDriven = picked.filter((item) => item.event_driven).length;
  const avg = estimate?.avg_cost_per_run ?? null;
  return { runs, perMonth, perYear, eventDriven, cost: avg === null ? null : avg * rawRuns, costDays: estimate?.cost_days ?? 30 };
}

/**
 * The rows left out when the plan is read: every automation that misses a
 * module. The backend refuses a pick that misses one, so such a row starts
 * unpicked with "Needs <module>" and never blocks the others.
 */
export function initiallyLeftOut(items: readonly FleetEnableItem[]): Set<string> {
  return new Set(items.filter((item) => item.missing.length > 0).map((item) => item.id));
}

/** The slots of a fleet bound and still to bind, from `GET /fleets/{id}/capability-slots`. */
export function slotCounts(members: readonly { slots: readonly { state: string }[] }[] | undefined): { bound: number; total: number; missing: number } {
  const slots = (members ?? []).flatMap((member) => member.slots);
  const bound = slots.filter((slot) => slot.state === "filled").length;
  return { bound, total: slots.length, missing: slots.length - bound };
}
