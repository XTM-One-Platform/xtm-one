// Reset a built-in fleet to its factory state (#1508): the dialog lists one
// line per non-zero count of the dry run and nothing for a fleet already reset.
import { describe, expect, it } from "vitest";

import type { FleetResetResult } from "@/hooks/api/useFleetApi";

import { resetPlanLines } from "./FleetResetDialog";

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
