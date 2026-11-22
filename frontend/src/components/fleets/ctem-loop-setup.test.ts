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
  signalTracePath,
  stepOf,
  testSignal,
} from "./ctem-loop-setup";
import { showsCtemLoopSetupEntry } from "./CtemLoopSetupEntry";

const step = (key: FleetSetupStep["key"], facts: Record<string, unknown> = {}, state: FleetSetupStep["state"] = "todo"): FleetSetupStep => ({ key, state, facts });

describe("ctem-loop-setup end (slice 4)", () => {
  it("builds a test signal for the real intake, marked as a test, a new dedupe key per send", () => {
    const a = testSignal(new Date("2026-11-22T06:30:00Z"));
    const b = testSignal(new Date("2026-11-22T06:31:00Z"));
    expect(a.trigger_path).toBe("new_vulnerability");
    expect(a.lens).toBe("world");
    expect(a.subject.kind).toBe("vulnerability");
    expect(a.evidence).toMatchObject({ test: true, origin: "ctem-loop-setup" });
    expect(a.dedupe_key).not.toBe(b.dedupe_key);
  });

  it("opens the command center on the signal's loop trace", () => {
    expect(signalTracePath("abc-1")).toBe("/?loop=signal%3Aabc-1");
  });

  it("shows the setup entry to an XTM One administrator only, never while impersonating", () => {
    expect(showsCtemLoopSetupEntry(true, true, false)).toBe(true);
    expect(showsCtemLoopSetupEntry(false, true, false)).toBe(false);
    expect(showsCtemLoopSetupEntry(true, false, false)).toBe(false);
    expect(showsCtemLoopSetupEntry(true, true, true)).toBe(false);
  });
});

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
