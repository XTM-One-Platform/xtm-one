// Reset a built-in fleet to its factory state (#1508): the dialog lists one
// line per non-zero count of the dry run and nothing for a fleet already reset.
import { describe, expect, it } from "vitest";

import type { FleetResetResult } from "@/hooks/api/useFleetApi";

import { resetHeldByStop, resetPlanLines } from "./fleet-reset";

const t = (key: string, values?: Record<string, unknown>) => (values ? `${key}|${JSON.stringify(values)}` : key);

function plan(over: Partial<FleetResetResult> = {}): FleetResetResult {
  return {
    fleet_id: "f",
    dry_run: true,
    automations: 0,
    run_as_cleared: 0,
    bindings: 0,
    pins: 0,
    gates: 0,
    autonomy_level: null,
    agents: 0,
    settings: [],
    documentation: false,
    held_by_stop: [],
    automations_state: { total: 0, enabled: 0, paused: 0, fleet_paused: 0 },
    ...over,
  };
}

describe("resetPlanLines", () => {
  it("lists nothing for a fleet already in its factory state", () => {
    expect(resetPlanLines(plan(), t as never)).toEqual([]);
  });

  it("lists one line per thing the reset changes, with its count", () => {
    const lines = resetPlanLines(
      plan({ automations: 3, bindings: 5, pins: 2, gates: 1, autonomy_level: "low", agents: 4, settings: ["health_enrolled"], documentation: true }),
      t as never,
    );
    expect(lines).toHaveLength(8);
    expect(lines[0]).toContain('"count":3');
    expect(lines.some((l) => l.startsWith("Autonomy level back"))).toBe(true);
    expect(lines.some((l) => l.startsWith("Documentation back"))).toBe(true);
  });
});

describe("resetHeldByStop", () => {
  // UX round 2 B1 on #1523: a fleet whose automations a stop holds is not in its factory state.
  it("names the automations an emergency stop holds, even when nothing else would change", () => {
    const held = plan({ held_by_stop: ["Daily triage", "Weekly digest"] });
    expect(resetPlanLines(held, t as never)).toEqual([]);
    expect(resetHeldByStop(held)).toEqual(["Daily triage", "Weekly digest"]);
  });

  it("is empty without a plan or without a stop", () => {
    expect(resetHeldByStop(undefined)).toEqual([]);
    expect(resetHeldByStop(plan())).toEqual([]);
  });
});
