import { describe, expect, it } from "vitest";

import type { FleetSetupStatus, FleetSetupStep } from "@/hooks/api/useFleetApi";

import {
  SETUP_ENTRY_DISMISS_KEY,
  SETUP_STEPS,
  SETUP_STEP_INTROS,
  SETUP_STEP_TITLES,
  connectedModules,
  countFact,
  dashboardSetupEntryShown,
  isSettled,
  loopRuns,
  nextStep,
  openingStep,
  previousStep,
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

  it("says the loop runs only when an automation of the fleet is on (UX r2 B1)", () => {
    const status = (enabled: unknown) => ({ steps: [step("enable", { automations_enabled: enabled, automations_total: 21 })] }) as unknown as FleetSetupStatus;
    expect(loopRuns(status(0))).toBe(false);
    expect(loopRuns(status("3"))).toBe(false);
    expect(loopRuns({ steps: [] } as unknown as FleetSetupStatus)).toBe(false);
    expect(loopRuns(undefined)).toBe(false);
    expect(loopRuns(status(1))).toBe(true);
    expect(loopRuns(status(21))).toBe(true);
  });

  it("shows the setup entry to an XTM One administrator only, never while impersonating", () => {
    expect(showsCtemLoopSetupEntry(true, true, false)).toBe(true);
    expect(showsCtemLoopSetupEntry(false, true, false)).toBe(false);
    expect(showsCtemLoopSetupEntry(true, false, false)).toBe(false);
    expect(showsCtemLoopSetupEntry(true, true, true)).toBe(false);
  });
});

describe("the command center's setup entry (Sam, 23 Nov 2026)", () => {
  const status = (enabled: number) => ({ steps: [step("enable", { automations_enabled: enabled, automations_total: 21 })] }) as unknown as FleetSetupStatus;
  const facts = (over: Partial<Parameters<typeof dashboardSetupEntryShown>[0]> = {}) => ({
    canSetUp: true,
    status: status(0),
    statusLoading: false,
    fleetAutomationsOn: [0, 0] as (number | null)[] | null,
    dismissed: null as string[] | null,
    ...over,
  });

  it("shows while no CTEM work runs, to an administrator who has not closed it", () => {
    expect(dashboardSetupEntryShown(facts())).toBe(true);
    expect(dashboardSetupEntryShown(facts({ fleetAutomationsOn: [] }))).toBe(true);
    expect(dashboardSetupEntryShown(facts({ dismissed: ["generic:other"] }))).toBe(true);
  });

  it("is gone once the CTEM loop fleet has an automation on", () => {
    expect(dashboardSetupEntryShown(facts({ status: status(1) }))).toBe(false);
    expect(dashboardSetupEntryShown(facts({ status: status(21) }))).toBe(false);
  });

  it("is gone once a custom CTEM fleet of the page runs an automation", () => {
    expect(dashboardSetupEntryShown(facts({ fleetAutomationsOn: [0, 3] }))).toBe(false);
  });

  it("is gone for good once the person closed it", () => {
    expect(dashboardSetupEntryShown(facts({ dismissed: [SETUP_ENTRY_DISMISS_KEY] }))).toBe(false);
  });

  it("is not drawn while the status or a fleet's automations are loading", () => {
    expect(dashboardSetupEntryShown(facts({ statusLoading: true }))).toBe(false);
    expect(dashboardSetupEntryShown(facts({ fleetAutomationsOn: null }))).toBe(false);
    expect(dashboardSetupEntryShown(facts({ fleetAutomationsOn: [0, null] }))).toBe(false);
  });

  it("is never drawn for a non-administrator", () => {
    expect(dashboardSetupEntryShown(facts({ canSetUp: false }))).toBe(false);
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
      ],
    };
    expect(countFact(stepOf(status, "organization"), "crown_jewels")).toBe(3);
    expect(countFact(stepOf(status, "organization"), "tech_stack")).toBe(0);
    expect(connectedModules(stepOf(status, "modules"))).toEqual(["opencti", "opencrq"]);
    expect(connectedModules(undefined)).toEqual([]);
    expect(stepOf(status, "enable")).toBeUndefined();
  });
});
