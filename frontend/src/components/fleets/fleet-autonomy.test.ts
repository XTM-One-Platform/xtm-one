import { describe, expect, it } from "vitest";

import type { FleetAutonomyGate } from "@/hooks/api/useFleetApi";

import {
  AUTONOMY_LEVELS,
  autonomyChipLabel,
  autonomyCounts,
  autonomyStatusLabel,
  becomesAutomatic,
  effectiveAutonomyLevel,
  isAutonomyRaise,
  selectableLevels,
} from "./fleet-autonomy";

// The backend matrix (`fleet_autonomy.is_gated`) for each impact, as `gate_catalog` returns it.
const gate = (action: string, impact: FleetAutonomyGate["impact"]): FleetAutonomyGate => {
  const from = { low: 0, high: 1, destructive: 2 }[impact];
  return {
    key: action.toLowerCase().replace(/ /g, "."),
    action,
    module: "platform",
    impact,
    gated: true,
    by_level: { full_gates: true, medium: from >= 1, low: from >= 2, autonomous: false },
  };
};

const catalog = [gate("Create a ticket", "low"), gate("Launch a scenario", "high"), gate("Delete an asset", "destructive")];

describe("fleet autonomy (#9769 S3)", () => {
  it("reads an unknown level as the most supervised one (fail closed)", () => {
    expect(effectiveAutonomyLevel(undefined)).toBe("full_gates");
    expect(effectiveAutonomyLevel("bogus")).toBe("full_gates");
    expect(effectiveAutonomyLevel("low")).toBe("low");
  });

  it("counts what runs without approval and what waits, per level", () => {
    expect(AUTONOMY_LEVELS.map((level) => autonomyCounts(catalog, level))).toEqual([
      { automatic: 0, waiting: 3 },
      { automatic: 1, waiting: 2 },
      { automatic: 2, waiting: 1 },
      { automatic: 3, waiting: 0 },
    ]);
  });

  it("lists the actions a raise makes automatic, none for a lowering", () => {
    expect(becomesAutomatic(catalog, "full_gates", "low").map((g) => g.action)).toEqual(["Create a ticket", "Launch a scenario"]);
    expect(becomesAutomatic(catalog, "low", "full_gates")).toEqual([]);
  });

  it("tells a raise from a lowering", () => {
    expect(isAutonomyRaise("full_gates", "medium")).toBe(true);
    expect(isAutonomyRaise("autonomous", "low")).toBe(false);
    expect(isAutonomyRaise("medium", "medium")).toBe(false);
  });

  it("offers an administrator every step, a manager the current one and below, a reader none", () => {
    expect(selectableLevels("medium", { canManage: true, isAdmin: true })).toEqual(AUTONOMY_LEVELS);
    expect(selectableLevels("medium", { canManage: true, isAdmin: false })).toEqual(["full_gates", "medium"]);
    expect(selectableLevels("medium", { canManage: false, isAdmin: false })).toEqual([]);
  });

  it("never shows a level the control does not enforce on a fleet that gates nothing (#10439)", () => {
    const t = (key: string, values?: Record<string, string | number>) =>
      key.replace(/\{(\w+)\}/g, (_m, name: string) => String(values?.[name] ?? ""));
    expect(autonomyChipLabel("full_gates", catalog, "medium_low", t)).toBe("Full human gates");
    expect(autonomyChipLabel("full_gates", [], null, t)).toBe("No gated actions");
    expect(autonomyChipLabel("full_gates", undefined, undefined, t)).toBe("No gated actions");
    expect(autonomyChipLabel("full_gates", [], "medium_low", t)).toBe("Between Medium and Low, by instruction");
  });

  it("gives the Fleets list card the About chip's words from the gate count (#10457)", () => {
    const t = (key: string, values?: Record<string, string | number>) =>
      key.replace(/\{(\w+)\}/g, (_m, name: string) => String(values?.[name] ?? ""));
    expect(autonomyStatusLabel("medium", 2, "low", t)).toBe("Medium (autonomy level)");
    expect(autonomyStatusLabel("autonomous", 0, null, t)).toBe("No gated actions");
    expect(autonomyStatusLabel("full_gates", undefined, undefined, t)).toBe("No gated actions");
    expect(autonomyStatusLabel("autonomous", 0, "medium_low", t)).toBe("Between Medium and Low, by instruction");
  });
});
