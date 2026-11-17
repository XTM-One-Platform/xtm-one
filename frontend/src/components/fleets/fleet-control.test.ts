import { describe, expect, it } from "vitest";

import { canControlFleet, fleetControlState } from "./fleet-control";

const counts = (total: number, enabled: number, paused = 0, fleet_paused = 0) => ({ total, enabled, paused, fleet_paused });

describe("fleetControlState (#9769 S2)", () => {
  it("is unknown while the health is not read", () => {
    expect(fleetControlState(undefined)).toBeNull();
    expect(fleetControlState(null)).toBeNull();
  });

  it("offers Enable fleet alone on a fleet nobody switched on (#9769 S4b, #9835 N1)", () => {
    expect(fleetControlState(counts(4, 0))).toEqual({
      optIn: "not_set_up",
      canResume: false,
      canPause: false,
      resumable: 0,
      canEnable: true,
      enableable: 4,
    });
  });

  it("offers no action on a fleet without automation", () => {
    expect(fleetControlState(counts(0, 0))).toMatchObject({ optIn: "not_set_up", canResume: false, canPause: false, canEnable: false });
  });

  it("offers Pause all and Enable the rest on a partially on fleet", () => {
    expect(fleetControlState(counts(3, 2))).toEqual({
      optIn: "partial",
      canResume: false,
      canPause: true,
      resumable: 0,
      canEnable: true,
      enableable: 1,
    });
  });

  it("offers Pause all alone on a fleet every automation of which is on", () => {
    expect(fleetControlState(counts(3, 3))).toMatchObject({ optIn: "on", canResume: false, canPause: true, canEnable: false });
  });

  it("offers Resume alone after the fleet's Pause all, with the count it restores", () => {
    expect(fleetControlState(counts(3, 0, 3, 3))).toMatchObject({ optIn: "paused", canResume: true, canPause: false, resumable: 3, canEnable: false });
  });

  it("offers Enable fleet on a fleet paused by a person: Resume would restore nothing", () => {
    expect(fleetControlState(counts(3, 0, 2, 0))).toMatchObject({ optIn: "paused", canResume: false, resumable: 0, canEnable: true });
  });

  it("offers Resume and Pause all when a Pause all left one automation switched on again by hand", () => {
    expect(fleetControlState(counts(3, 1, 2, 2))).toMatchObject({ optIn: "partial", canResume: true, canPause: true });
  });
});

describe("canControlFleet", () => {
  const fleet = (over: Partial<{ can_manage: boolean; is_builtin: boolean; is_archived: boolean }> = {}) => ({
    can_manage: true,
    is_builtin: false,
    is_archived: false,
    ...over,
  });

  it("lets a manager switch a custom fleet", () => {
    expect(canControlFleet(fleet(), false)).toBe(true);
  });
  it("keeps a built-in fleet's automations for administrators, as the backend does", () => {
    expect(canControlFleet(fleet({ is_builtin: true }), false)).toBe(false);
    expect(canControlFleet(fleet({ is_builtin: true }), true)).toBe(true);
  });
  it("gives no control to a reader who does not manage the fleet, or on an archived fleet", () => {
    expect(canControlFleet(fleet({ can_manage: false }), true)).toBe(false);
    expect(canControlFleet(fleet({ is_archived: true }), true)).toBe(false);
  });
});
