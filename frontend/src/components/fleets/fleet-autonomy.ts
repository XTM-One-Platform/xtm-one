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
 * The fleet autonomy control (#9769 S3, design round 4 APPROVE): the four
 * levels, what each one makes automatic for THIS fleet, and who may move it.
 * Pure: nothing here calls the API. The catalog is the backend's
 * (`GET /fleets/{id}/documentation` `autonomy_catalog`, from
 * `fleet_autonomy.gate_catalog`), so the strip and the run-time gate read one
 * matrix and the control is never a black box.
 */
import type { FleetApprovalGate, FleetAutonomyGate, FleetAutonomyLevel, FleetInstructionsPosition } from "@/hooks/api/useFleetApi";
import { INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";

/** From the most supervised to the least. */
export const AUTONOMY_LEVELS: FleetAutonomyLevel[] = ["full_gates", "medium", "low", "autonomous"];

/**
 * Catalog keys of the level names. Medium and Low have their own keys: the
 * plain "Medium" / "Low" keys are a duration and a severity elsewhere (fr
 * "Moyen terme"), never a level of approval (UX round 1 B4 on #9887).
 */
export const AUTONOMY_LEVEL_LABELS: Record<FleetAutonomyLevel, string> = {
  full_gates: "Full human gates",
  medium: "Medium (autonomy level)",
  low: "Low (autonomy level)",
  autonomous: "Full autonomous",
};

/** One sentence per level: what still waits for a person. */
export const AUTONOMY_LEVEL_HINTS: Record<FleetAutonomyLevel, string> = {
  full_gates: "Every action that writes outside XTM One waits for approval. Drafts and reads need none.",
  medium: "Low-impact writes run without approval; launches and deployments still wait.",
  low: "Only destructive actions and actions on production assets wait.",
  autonomous: "Nothing waits for approval. Every action is still logged and reported in Operations.",
};

/** A stored value as a level: anything unknown reads as the most supervised one (fail closed, as the backend). */
export function effectiveAutonomyLevel(value: string | null | undefined): FleetAutonomyLevel {
  return AUTONOMY_LEVELS.includes(value as FleetAutonomyLevel) ? (value as FleetAutonomyLevel) : "full_gates";
}

/** Whether moving from `from` to `to` makes the fleet more autonomous (administrator only). */
export function isAutonomyRaise(from: FleetAutonomyLevel, to: FleetAutonomyLevel): boolean {
  return AUTONOMY_LEVELS.indexOf(to) > AUTONOMY_LEVELS.indexOf(from);
}

/** At `level`: how many of the fleet's gated actions run without approval and how many wait. */
export function autonomyCounts(catalog: FleetAutonomyGate[], level: FleetAutonomyLevel): { automatic: number; waiting: number } {
  const waiting = catalog.filter((gate) => gate.by_level?.[level] ?? true).length;
  return { automatic: catalog.length - waiting, waiting };
}

/** Catalog keys of the impact classes, one sentence-case label each (UX design round 3 A1). */
export const IMPACT_LABELS: Record<FleetAutonomyGate["impact"], string> = {
  low: "Low impact",
  high: "High impact",
  destructive: "Destructive",
};

/**
 * The plain action of a gate proposed from an integration's write tools, per
 * impact class (UX round 1 B2 on #10034): the same sentences as the backend's
 * `fleet_gate_proposals.IMPACT_ACTIONS`, translated here. @i18n-keys
 */
const IMPACT_ACTION_LABELS = {
  destructive: "Delete, or send in a person's name, in {provider}",
  high: "Create or change content in {provider}",
  low: "Mark, label or draft in {provider}",
} as const satisfies Record<FleetAutonomyGate["impact"], string>;

/**
 * The actions the built-in fleets declare (`builtin_fleet_docs`), so the page
 * shows them translated (UX round 2 B3 on #10034); a custom fleet's own
 * declared action reads as written. @i18n-keys
 */
const DECLARED_GATE_ACTIONS = [
  "Create a remediation ticket",
  "Launch an OpenAEV scenario",
  "Validate exploitability in OpenAEV",
  "Run a test against an AI system",
  "Any proposed action on a supplier",
  // The CTEM loop gate catalog (#916, CTEM-34: `builtin_fleet_docs._ctem_gates`).
  "Validate: create or launch an OpenAEV scenario or atomic test",
  "Review: an analyst checks the threat package",
  "Decide: choose the remediation option",
  "Mobilize: create the tickets, notify the owners, hand over the prepared change",
  "Mobilize: carry out an action on a supplier (contact, control, clause, monitoring)",
  "Apply a change on production through an integration",
  "Accept a residual risk beyond the policy cap",
  "Close a case without a passing re-test",
] as const;

/**
 * One line per CTEM loop gate action (#916, CTEM-34): what happens when it
 * runs alone, so a person sees the effect of a pin before the click. Keyed
 * by the declared action, not the gate key: two actions share `ctem.mobilize`
 * (the case's tickets, and the third-party fleet's supplier action), and each
 * says only what it does (UX round 1 B2 on #934). @i18n-keys
 */
const CTEM_GATE_EFFECTS: Readonly<Record<string, string>> = {
  "Validate: create or launch an OpenAEV scenario or atomic test":
    "When it runs alone, OpenAEV scenarios and atomic tests are created and launched without a person.",
  "Review: an analyst checks the threat package": "When it runs alone, the fleet requests the coverage without an analyst's review.",
  "Decide: choose the remediation option": "When it runs alone, the recommended option is applied and logged as decided by policy.",
  "Mobilize: create the tickets, notify the owners, hand over the prepared change":
    "When it runs alone, the tickets, the owners' notices and the prepared change of a case go out without a person.",
  "Mobilize: carry out an action on a supplier (contact, control, clause, monitoring)":
    "When it runs alone, the fleet carries out the action on the supplier without a person.",
  "Apply a change on production through an integration":
    "When it runs alone, the fleet applies a change on production through an integration.",
  "Accept a residual risk beyond the policy cap": "When it runs alone, a residual risk beyond the policy cap is accepted without a person.",
  "Close a case without a passing re-test": "When it runs alone, a case closes on the proof that fits the fix, without a re-test.",
} as const;

/** The one-line effect of a CTEM loop gate, read from its action; `null` for any other gate. */
export function ctemGateEffect(gate: Pick<FleetAutonomyGate, "key" | "action">): string | null {
  if (!gate.key.startsWith("ctem.")) return null;
  return CTEM_GATE_EFFECTS[gate.action] ?? null;
}

/**
 * Whether a CTEM loop gate waits out of the box: the seeded level (Low)
 * gates destructive actions only, so the one destructive gate, Mobilize,
 * waits and every other runs (`fleet_autonomy.is_gated`). Read from the
 * gate's impact, never a list of keys, so the backend catalog stays the source.
 */
export function ctemGateWaitsByDefault(gate: Pick<FleetAutonomyGate, "impact">): boolean {
  return gate.impact === "destructive";
}

/** The per-action settings of a gate, in the order the control lists them (`fleet_autonomy.APPROVAL_PINS`). @i18n-keys */
export const GATE_PIN_LABELS = {
  level: "Follows the level",
  wait: "Always waits",
  run: "Always runs alone",
} as const;

/**
 * The pins after setting `key` to `pin` (`null`: follow the level), the
 * other gates' pins kept: `PUT /fleets/{id}/autonomy` takes the whole map.
 */
export function nextPins(catalog: FleetAutonomyGate[], key: string, pin: "wait" | "run" | null): Record<string, "wait" | "run"> {
  const out: Record<string, "wait" | "run"> = {};
  for (const gate of catalog) {
    if (gate.key !== key && (gate.pin === "wait" || gate.pin === "run")) out[gate.key] = gate.pin;
  }
  if (pin) out[key] = pin;
  return out;
}

/**
 * Whether moving a gate's pin from `from` to `to` lets the action run with
 * less approval: a new "run", or an "Always waits" removed. An administrator
 * only, as the backend's `pins_lower_approval` (403 otherwise).
 */
export function pinLowersApproval(from: "wait" | "run" | null | undefined, to: "wait" | "run" | null): boolean {
  return (to === "run" && from !== "run") || (from === "wait" && to !== "wait");
}

/** The display name of an integration provider (`microsoft` reads "Microsoft"). */
export function integrationName(provider: string): string {
  return INTEGRATION_PROVIDER_NAMES[provider] ?? provider;
}

/**
 * A gate's action as the page shows it: a gate proposed from an integration
 * (it carries `provider`) reads as its translated impact sentence with the
 * integration's name (one gate per integration and impact, never numbered
 * parts: UX round 2 B1); a declared gate of a built-in fleet reads translated,
 * any other as written.
 */
export function gateActionLabel(
  gate: Pick<FleetApprovalGate, "key" | "action" | "impact" | "provider">,
  t: (key: string, values?: Record<string, string | number>) => string,
): string {
  if (!gate.provider) {
    return (DECLARED_GATE_ACTIONS as readonly string[]).includes(gate.action) ? t(gate.action) : gate.action;
  }
  return t(IMPACT_ACTION_LABELS[gate.impact], { provider: integrationName(gate.provider) });
}

/** The named actions of `gates`, joined for one line (a step tip, a toast). */
export function gateNames(
  gates: Pick<FleetApprovalGate, "key" | "action" | "impact" | "provider">[],
  t: (key: string, values?: Record<string, string | number>) => string,
): string {
  return gates.map((gate) => gateActionLabel(gate, t)).join("; ");
}

/**
 * Where a fleet's instructions put it on the same scale, between-step positions
 * included (#10168, `fleet_autonomy.INSTRUCTIONS_POSITIONS`), most supervised first.
 */
export const INSTRUCTIONS_POSITIONS: FleetInstructionsPosition[] = [
  "full_gates",
  "full_gates_medium",
  "medium",
  "medium_low",
  "low",
  "low_autonomous",
  "autonomous",
];

/** Catalog keys of the instructions positions. @i18n-keys */
export const INSTRUCTIONS_POSITION_LABELS = {
  full_gates: "Full human gates",
  full_gates_medium: "Between Full human gates and Medium",
  medium: "Medium (autonomy level)",
  medium_low: "Between Medium and Low",
  low: "Low (autonomy level)",
  low_autonomous: "Between Low and Full autonomous",
  autonomous: "Full autonomous",
} as const satisfies Record<FleetInstructionsPosition, string>;

/**
 * What the fleet's status chip says (#10439): the enforced level only when the
 * control gates at least one declared action; on a fleet that gates nothing,
 * the instructions position when one is set, else "No gated actions". Never a
 * level the platform does not enforce.
 */
export function autonomyChipLabel(
  level: FleetAutonomyLevel,
  catalog: readonly FleetAutonomyGate[] | undefined,
  position: FleetInstructionsPosition | null | undefined,
  t: (key: string, values?: Record<string, string | number>) => string,
): string {
  return autonomyStatusLabel(level, catalog?.length ?? 0, position, t);
}

/**
 * The same status from a count of gated actions (#10457): the Fleets list card
 * reads `gated_action_count` from `GET /fleets`, never the catalog per fleet.
 */
export function autonomyStatusLabel(
  level: FleetAutonomyLevel,
  gatedCount: number | null | undefined,
  position: FleetInstructionsPosition | null | undefined,
  t: (key: string, values?: Record<string, string | number>) => string,
): string {
  if ((gatedCount ?? 0) > 0) return t(AUTONOMY_LEVEL_LABELS[level]);
  if (position) return t("{position}, by instruction", { position: t(INSTRUCTIONS_POSITION_LABELS[position]) });
  return t("No gated actions");
}

/** Whether moving the instructions position from `from` to `to` is toward more autonomy (administrator only). */
export function isInstructionsRaise(from: FleetInstructionsPosition | null | undefined, to: FleetInstructionsPosition): boolean {
  const before = from ? INSTRUCTIONS_POSITIONS.indexOf(from) : 0;
  return INSTRUCTIONS_POSITIONS.indexOf(to) > before;
}

/** The actions that wait at `from` and run without approval at `to`: what a raise lists before it applies. */
export function becomesAutomatic(catalog: FleetAutonomyGate[], from: FleetAutonomyLevel, to: FleetAutonomyLevel): FleetAutonomyGate[] {
  return catalog.filter((gate) => (gate.by_level?.[from] ?? true) && !(gate.by_level?.[to] ?? true));
}

/** The actions that run alone at `from` and wait for a person at `to`: what adding approval back changes. */
export function becomesGated(catalog: FleetAutonomyGate[], from: FleetAutonomyLevel, to: FleetAutonomyLevel): FleetAutonomyGate[] {
  return becomesAutomatic(catalog, to, from);
}

/**
 * Which steps the reader may pick: an administrator any; whoever manages the
 * fleet (a built-in one: an administrator) the current level and the ones
 * below it, a raise being refused by the backend (403). A reader who may not
 * manage it: none, the strip is shown read only.
 */
export function selectableLevels(
  current: FleetAutonomyLevel,
  { canManage, isAdmin }: { canManage: boolean; isAdmin: boolean },
): FleetAutonomyLevel[] {
  if (!canManage) return [];
  if (isAdmin) return [...AUTONOMY_LEVELS];
  return AUTONOMY_LEVELS.filter((level) => !isAutonomyRaise(current, level));
}
