import { describe, expect, it } from "vitest";

import type { FleetSetupStatus, FleetSetupStep } from "@/hooks/api/useFleetApi";

import {
  SETUP_STEPS,
  SETUP_STEP_INTROS,
  SETUP_STEP_TITLES,
  connectedModules,
  countFact,
  ecosystemSlots,
  isSettled,
  nextStep,
  openingStep,
  previousStep,
  stepOf,
} from "./ctem-loop-setup";

const step = (key: FleetSetupStep["key"], facts: Record<string, unknown> = {}, state: FleetSetupStep["state"] = "todo"): FleetSetupStep => ({ key, state, facts });

describe("ctem-loop-setup", () => {
  it("walks the six steps of organization_setup_prompt.STEPS in order, each with a title and an intro", () => {
    expect(SETUP_STEPS).toEqual(["organization", "modules", "ecosystem", "gates", "preview", "enable"]);
    for (const key of SETUP_STEPS) {
      expect(SETUP_STEP_TITLES[key]).toBeTruthy();
      expect(SETUP_STEP_INTROS[key]).toBeTruthy();
    }
  });

  it("opens on the server's resume step, else on the first", () => {
    expect(openingStep({ resume_on: "gates" })).toBe("gates");
    expect(openingStep({ resume_on: null })).toBe("organization");
    expect(openingStep(undefined)).toBe("organization");
  });

  it("moves forward and back without leaving the list", () => {
    expect(nextStep("organization")).toBe("modules");
    expect(nextStep("enable")).toBeNull();
    expect(previousStep("modules")).toBe("organization");
    expect(previousStep("organization")).toBeNull();
  });

  it("counts done and skipped as settled, never ready or todo", () => {
    expect(isSettled("done")).toBe(true);
    expect(isSettled("skipped")).toBe(true);
    expect(isSettled("ready")).toBe(false);
    expect(isSettled("todo")).toBe(false);
    expect(isSettled(undefined)).toBe(false);
  });

  it("reads the step facts defensively", () => {
    const status: FleetSetupStatus = {
      version: "ctem-loop-1",
      fleet_id: "f1",
      resume_on: "modules",
      steps: [
        step("organization", { crown_jewels: 3, tech_stack: "x" }),
        step("modules", { connected: ["opencrq", "opencti", "other"] }),
        step("ecosystem", { slots: { ticketing: "filled", scanner: "empty", edr_xdr: "weird" } }),
      ],
    };
    expect(countFact(stepOf(status, "organization"), "crown_jewels")).toBe(3);
    expect(countFact(stepOf(status, "organization"), "tech_stack")).toBe(0);
    expect(connectedModules(stepOf(status, "modules"))).toEqual(["opencti", "opencrq"]);
    expect(connectedModules(undefined)).toEqual([]);
    expect(ecosystemSlots(stepOf(status, "ecosystem"))).toEqual({
      edr_xdr: "unavailable",
      ticketing: "filled",
      scanner: "empty",
      notification: "unavailable",
    });
    expect(stepOf(status, "enable")).toBeUndefined();
  });
});
