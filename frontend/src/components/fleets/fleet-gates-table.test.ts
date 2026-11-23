import { describe, expect, it } from "vitest";

import type { FleetAutonomyGate } from "@/hooks/api/useFleetApi";

import { GATE_IMPACT_EXPLANATIONS, gateRows, gateStateAt } from "./fleet-gates-table";

const levels = (fullGates: boolean, medium: boolean, low: boolean, autonomous: boolean) => ({
  full_gates: fullGates,
  medium,
  low,
  autonomous,
});

const gate = (key: string, impact: FleetAutonomyGate["impact"], byLevel: FleetAutonomyGate["by_level"], pin?: FleetAutonomyGate["pin"]) =>
  ({ key, action: key, module: "opencti", impact, gated: true, by_level: byLevel, pin }) as FleetAutonomyGate;

describe("fleet gates table (#1373)", () => {
  it("reads the state of a gate at the level picked, live", () => {
    const g = gate("a", "high", levels(true, true, false, false));
    expect(gateStateAt(g, "full_gates")).toBe("waits");
    expect(gateStateAt(g, "low")).toBe("runs");
  });

  it("lets a pin win over the level and fails closed on a missing level", () => {
    expect(gateStateAt(gate("a", "low", levels(false, false, false, false), "wait"), "autonomous")).toBe("waits");
    expect(gateStateAt(gate("b", "destructive", levels(true, true, true, true), "run"), "full_gates")).toBe("runs");
    expect(gateStateAt({ by_level: {} as FleetAutonomyGate["by_level"] }, "medium")).toBe("waits");
  });

  it("puts what waits first, then the most harmful first", () => {
    const catalog = [
      gate("low-wait", "low", levels(true, true, true, true)),
      gate("destructive-runs", "destructive", levels(false, false, false, false)),
      gate("destructive-wait", "destructive", levels(true, true, true, true)),
      gate("high-wait", "high", levels(true, true, true, true)),
    ];
    expect(gateRows(catalog, "medium").map((row) => row.gate.key)).toEqual([
      "destructive-wait",
      "high-wait",
      "low-wait",
      "destructive-runs",
    ]);
  });

  it("explains every impact class", () => {
    for (const impact of ["low", "high", "destructive"] as const) expect(GATE_IMPACT_EXPLANATIONS[impact]).toMatch(/outside XTM One/);
  });
});
