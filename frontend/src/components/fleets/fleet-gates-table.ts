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
 * Pure logic of the gates table of the setup step (#1373): one row per gated
 * action, what it writes and why it matters, and whether it waits at the level
 * picked. Read from the same catalog as the strip and the matrix
 * (`autonomy_catalog`), so the three never disagree.
 */
import type { FleetAutonomyGate, FleetAutonomyLevel } from "@/hooks/api/useFleetApi";

/** What a gate of each impact class writes, and why a person may want to see it first. @i18n-keys */
export const GATE_IMPACT_EXPLANATIONS = {
  destructive: "Deletes data or speaks in a person's name outside XTM One; it cannot be taken back.",
  high: "Creates or changes content outside XTM One that other people and systems act on.",
  low: "A reversible mark, label or draft outside XTM One.",
} as const satisfies Record<FleetAutonomyGate["impact"], string>;

/** The two states of a row at a level, by meaning (13bis.10). @i18n-keys */
export const GATE_STATE_LABELS = {
  waits: "Waits for approval",
  runs: "Runs alone",
} as const;

export type GateRowState = keyof typeof GATE_STATE_LABELS;

/** A pin wins over the level; absent, the gate follows the level (`by_level`), failing closed. */
export function gateStateAt(gate: Pick<FleetAutonomyGate, "by_level" | "pin">, level: FleetAutonomyLevel): GateRowState {
  if (gate.pin === "wait") return "waits";
  if (gate.pin === "run") return "runs";
  return (gate.by_level?.[level] ?? true) ? "waits" : "runs";
}

const IMPACT_ORDER: Record<FleetAutonomyGate["impact"], number> = { destructive: 0, high: 1, low: 2 };

/** The rows in reading order: what waits first, then by impact, the most harmful first. */
export function gateRows(
  catalog: readonly FleetAutonomyGate[],
  level: FleetAutonomyLevel,
): { gate: FleetAutonomyGate; state: GateRowState }[] {
  return catalog
    .map((gate) => ({ gate, state: gateStateAt(gate, level) }))
    .sort(
      (a, b) =>
        Number(a.state === "runs") - Number(b.state === "runs") || IMPACT_ORDER[a.gate.impact] - IMPACT_ORDER[b.gate.impact],
    );
}
