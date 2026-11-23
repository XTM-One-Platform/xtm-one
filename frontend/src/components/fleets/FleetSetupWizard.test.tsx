// The fleet setup wizard (#1151): the Enable fleet dialog's assertions moved to its
// "Run as and automations" and "Review and enable" steps (dry run before confirm,
// partial pick, missing-module notice, count on Confirm, nothing enabled on open),
// plus the steps a fleet walks and the client-side resume.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { FleetEnableResult } from "@/hooks/api/useFleetApi";
import { renderWithProviders } from "@/testing/providers";

import { FleetSetupWizard } from "./FleetSetupWizard";
import {
  initiallyLeftOut,
  openingWizardStep,
  pickedLoad,
  pickedPlan,
  providerName,
  readWizardProgress,
  slotCounts,
  wizardSteps,
  writeWizardProgress,
} from "./fleet-setup-wizard";

const api = vi.hoisted(() => ({
  plan: undefined as FleetEnableResult | undefined,
  quota: [{ trigger: "New threat", runs_min: 2, runs_max: 16 }] as { trigger: string; runs_min: number; runs_max: number }[] | undefined,
  slots: { fleet_id: "f1", members: [] as { member_id: string; slots: { state: string }[] }[] },
  mutateAsync: vi.fn(async () => ({ changed: 2 })),
  aiOff: false,
}));
vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => api.aiOff,
  useAiDisabledReason: () => (api.aiOff ? "AI is disabled" : null),
}));
vi.mock("@/hooks/api/useFleetApi", () => ({
  useFleet: () => ({ data: { id: "f1", name: "AI security", autonomy_level: "full_gates" } }),
  useFleetCapabilitySlots: () => ({ data: api.slots, isError: false, refetch: vi.fn() }),
  useFleetEnablePlan: () => ({ data: api.plan, error: null }),
  useFleetDocumentation: () => ({ data: { structured: { quota: api.quota } }, isError: false }),
  useFleetEnable: () => ({ mutateAsync: api.mutateAsync, isPending: false }),
}));
vi.mock("./FleetAutonomyStrip", () => ({ FleetAutonomyStrip: () => <div data-testid="autonomy-strip" /> }));
vi.mock("./FleetCapabilitySlotsCard", () => ({ FleetCapabilitySlotsCard: () => <div data-testid="slots-card" /> }));
vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "u1", email: "ana@example.com", full_name: "Ana" }, isAdmin: false }),
}));

const plan = (over: Partial<FleetEnableResult> = {}): FleetEnableResult => ({
  fleet_id: "f1",
  dry_run: true,
  run_as_user_id: "u1",
  changed: 2,
  ready: true,
  missing: [],
  items: [
    { id: "a1", name: "Daily exposure review", providers: ["opencti"], missing: [], selected: true },
    { id: "a2", name: "Weekly simulation", providers: ["openaev"], missing: [], selected: true },
  ],
  automations: { total: 2, enabled: 0, paused: 0, fleet_paused: 0 },
  ...over,
});
const missingPlan = () =>
  plan({
    ready: false,
    missing: ["openaev"],
    items: [
      { id: "a1", name: "Daily exposure review", providers: ["opencti"], missing: [], selected: true },
      { id: "a2", name: "Weekly simulation", providers: ["openaev"], missing: ["openaev"], selected: true },
    ],
  });

beforeEach(() => {
  window.localStorage.clear();
  api.plan = plan();
  api.quota = [{ trigger: "New threat", runs_min: 2, runs_max: 16 }];
  api.slots = { fleet_id: "f1", members: [] };
  api.aiOff = false;
  api.mutateAsync.mockClear();
});
afterEach(cleanup);

const open = (startAt?: "automations" | "review") =>
  renderWithProviders(
    <MemoryRouter>
      <FleetSetupWizard fleetId="f1" fleetName="AI security" open startAt={startAt} onOpenChange={() => undefined} />
    </MemoryRouter>,
  );
const next = () => fireEvent.click(screen.getByTestId("fleet-setup-next"));

describe("FleetSetupWizard", () => {
  it("opens on What it does with the run estimate, and writes nothing on opening", () => {
    open();
    expect(screen.getByRole("dialog", { name: "Set up AI security" })).toBeInTheDocument();
    expect(screen.getByTestId("fleet-setup-wizard")).toHaveAttribute("data-step", "about");
    expect(screen.getByTestId("fleet-setup-estimate")).toHaveTextContent("2 to 16 runs a month, from the fleet's documented triggers");
    expect(api.mutateAsync).not.toHaveBeenCalled();
  });

  it("skips Integrations for a fleet without slots and walks to Review", () => {
    open();
    next();
    expect(screen.getByTestId("fleet-setup-wizard")).toHaveAttribute("data-step", "autonomy");
    next();
    expect(screen.getByTestId("fleet-setup-wizard")).toHaveAttribute("data-step", "automations");
    next();
    expect(screen.getByTestId("fleet-setup-wizard")).toHaveAttribute("data-step", "review");
    expect(api.mutateAsync).not.toHaveBeenCalled();
  });

  it("offers I will do it later on Integrations when the fleet has slots", () => {
    api.slots = { fleet_id: "f1", members: [{ member_id: "m1", slots: [{ state: "empty" }] }] };
    open();
    next();
    expect(screen.getByTestId("fleet-setup-wizard")).toHaveAttribute("data-step", "integrations");
    fireEvent.click(screen.getByTestId("fleet-setup-later"));
    expect(screen.getByTestId("fleet-setup-wizard")).toHaveAttribute("data-step", "autonomy");
    expect(readWizardProgress("f1")).toMatchObject({ step: "autonomy", skippedIntegrations: true });
  });

  it("shows the run-as user tied to its helper line", () => {
    open("automations");
    const runAs = screen.getByRole("textbox", { name: "Run as" });
    expect(runAs).toHaveValue("You (Ana)");
    expect(runAs).toHaveAccessibleDescription("Its automations use this user's connected modules, model and memory.");
    expect(screen.getByText("Daily exposure review")).toBeInTheDocument();
  });

  it("enables in one call on Confirm, as the caller", async () => {
    open("review");
    fireEvent.click(screen.getByRole("button", { name: "Enable 2 automations" }));
    await waitFor(() => expect(api.mutateAsync).toHaveBeenCalledTimes(1));
    expect(api.mutateAsync).toHaveBeenCalledWith({ runAs: null, assignmentIds: undefined });
    expect(await screen.findByTestId("fleet-setup-done")).toBeInTheDocument();
    expect(readWizardProgress("f1")).toBeNull();
  });

  it("enables only the picked automations when one is left out", async () => {
    open("automations");
    const rows = screen.getAllByRole("checkbox");
    expect(rows).toHaveLength(3);
    rows.forEach((box) => expect(box).toHaveAttribute("aria-checked", "true"));
    fireEvent.click(screen.getByRole("checkbox", { name: "Switch on Weekly simulation" }));
    expect(screen.getByRole("checkbox", { name: "Select every automation that is off" })).toHaveAttribute("aria-checked", "mixed");
    next();
    fireEvent.click(screen.getByRole("button", { name: "Enable 1 automation" }));
    await waitFor(() => expect(api.mutateAsync).toHaveBeenCalledTimes(1));
    expect(api.mutateAsync).toHaveBeenCalledWith({ runAs: null, assignmentIds: ["a1"] });
  });

  it("shows the expected runs a day and cost of the pick, and follows the pick (#1538)", () => {
    api.plan = plan({
      items: [
        { id: "a1", name: "Daily exposure review", providers: ["opencti"], missing: [], selected: true, runs_per_day: 24, event_driven: false },
        { id: "a2", name: "Weekly simulation", providers: ["openaev"], missing: [], selected: true, runs_per_day: 1, event_driven: false },
        { id: "a3", name: "Failure observer", providers: [], missing: [], selected: true, runs_per_day: 0, event_driven: true },
      ],
      estimate: { scheduled_runs_per_day: 25, event_automations: 1, avg_cost_per_run: 0.05, cost_per_day: 1.25, cost_days: 30 },
    });
    open("automations");
    const load = screen.getByTestId("fleet-enable-load");
    expect(load).toHaveTextContent("About 25 scheduled runs a day.");
    expect(load).toHaveTextContent("1 automation runs once per event, as often as its events arrive.");
    expect(load).toHaveTextContent("About $1.25 a day for the scheduled runs, from the mean cost of their runs over the last 30 days.");
    fireEvent.click(screen.getByRole("checkbox", { name: "Switch on Daily exposure review" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Switch on Failure observer" }));
    expect(screen.getByTestId("fleet-enable-load")).toHaveTextContent("About 1 scheduled run a day.");
    expect(screen.getByTestId("fleet-enable-load")).not.toHaveTextContent("once per event");
    next();
    expect(screen.getByTestId("fleet-setup-review-load")).toHaveTextContent("About $0.05 a day for the scheduled runs");
  });

  it("says there is no cost yet without a run history, and no scheduled run at 0", () => {
    api.plan = plan({
      items: [{ id: "a3", name: "Failure observer", providers: [], missing: [], selected: true, runs_per_day: 0, event_driven: true }],
      estimate: { scheduled_runs_per_day: 0, event_automations: 1, avg_cost_per_run: null, cost_per_day: null, cost_days: 30 },
    });
    open("automations");
    const load = screen.getByTestId("fleet-enable-load");
    expect(load).toHaveTextContent("No scheduled run a day.");
    expect(load).toHaveTextContent("No cost yet: these automations have not run in the last 30 days.");
  });

  it("says a monthly-only pick per month, never as no run, with the exact cost in a Tip, not a title (#1550 B2, B3)", () => {
    api.plan = plan({
      items: [{ id: "m1", name: "Monthly report", providers: [], missing: [], selected: true, runs_per_day: 0.0333, event_driven: false }],
      estimate: { scheduled_runs_per_day: 0.0333, event_automations: 0, avg_cost_per_run: 1.5, cost_per_day: 0.05, cost_days: 30 },
    });
    open("automations");
    const load = screen.getByTestId("fleet-enable-load");
    expect(load).toHaveTextContent("About 1 scheduled run a month.");
    expect(load).not.toHaveTextContent("No scheduled run");
    expect(load).toHaveTextContent("About $1.50 a month for the scheduled runs");
    // An exact figure needs no hover: no Tip, no tab stop.
    expect(screen.queryByTestId("fleet-enable-load-cost")).toBeNull();
  });

  it("gives a compacted cost its exact amount through a focusable Tip, never a native title (#1550 B2)", () => {
    api.plan = plan({
      items: [{ id: "a1", name: "Daily exposure review", providers: [], missing: [], selected: true, runs_per_day: 24, event_driven: false }],
      estimate: { scheduled_runs_per_day: 24, event_automations: 0, avg_cost_per_run: 50, cost_per_day: 1200, cost_days: 30 },
    });
    open("automations");
    expect(screen.getByTestId("fleet-enable-load-cost")).toHaveAttribute("tabindex", "0");
    next();
    const review = screen.getByTestId("fleet-setup-review-load");
    expect(review.querySelector("[title]")).toBeNull();
    expect(review.querySelector('[data-testid="fleet-enable-load-cost"]')).toHaveAttribute("tabindex", "0");
  });

  it("keeps Confirm disabled with nothing picked", () => {
    open("automations");
    fireEvent.click(screen.getByRole("checkbox", { name: "Select every automation that is off" }));
    expect(screen.getByText("Pick at least one automation to switch on.")).toBeInTheDocument();
    next();
    expect(screen.getByRole("button", { name: "Enable fleet" })).toBeDisabled();
  });

  it("leaves the automation that misses a module off, marked, and never blocks the others", () => {
    api.plan = missingPlan();
    open("automations");
    const rows = screen.getByTestId("fleet-enable-items").querySelectorAll("li");
    expect(rows[0]).toHaveAttribute("data-selected", "true");
    expect(rows[1]).toHaveAttribute("data-missing", "true");
    expect(rows[1]).toHaveAttribute("data-selected", "false");
    expect(rows[1].querySelector("[data-testid='fleet-enable-item-missing']")).toHaveTextContent("Needs OpenAEV");
    expect(screen.queryByTestId("fleet-enable-missing")).not.toBeInTheDocument();
    next();
    expect(screen.getByRole("button", { name: "Enable 1 automation" })).toBeEnabled();
  });

  it("names the module in the notice and disables Confirm when the person picks it anyway", () => {
    api.plan = missingPlan();
    open("automations");
    fireEvent.click(screen.getByRole("checkbox", { name: "Switch on Weekly simulation" }));
    expect(screen.getByRole("alert")).toHaveTextContent("You have not connected OpenAEV. Connect it in Integrations");
    expect(screen.getByRole("alert")).toHaveTextContent("Weekly simulation needs OpenAEV.");
    expect(screen.getByRole("link", { name: "Open Integrations" })).toHaveAttribute("href", "/integrations");
    next();
    expect(screen.getByRole("button", { name: "Enable 2 automations" })).toBeDisabled();
  });

  it("says when no estimate is written for the fleet", () => {
    api.quota = undefined;
    open();
    expect(screen.getByTestId("fleet-setup-estimate")).toHaveTextContent("No estimate written for this fleet");
  });

  it("says nothing is off and offers no count when every automation is on", () => {
    api.plan = plan({ changed: 0, ready: false, items: [] });
    open("automations");
    expect(screen.getByText("Nothing to enable: every automation of this fleet is on.")).toBeInTheDocument();
    next();
    expect(screen.getByRole("button", { name: "Enable fleet" })).toBeDisabled();
  });

  it("keeps Confirm disabled while AI is off (the enable endpoint is AI gated)", () => {
    api.aiOff = true;
    open("review");
    expect(screen.getByRole("button", { name: "Enable 2 automations" })).toBeDisabled();
  });

  it("opened from a parent flow, counts and shows only its own steps under the parent's title (UX r1 B1)", () => {
    renderWithProviders(
      <MemoryRouter>
        <FleetSetupWizard fleetId="f1" fleetName="CTEM loop" open startAt="automations" title="Set up the CTEM loop" onOpenChange={() => undefined} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("dialog", { name: "Set up the CTEM loop" })).toBeInTheDocument();
    expect(screen.getByText("Step 1 of 2")).toBeInTheDocument();
    expect(screen.queryByTestId("fleet-setup-dot-about")).not.toBeInTheDocument();
  });

  it("says how many automations switch on in a sentence on Review (UX r1 B3)", () => {
    open("review");
    expect(screen.getByTestId("fleet-setup-review")).toHaveTextContent("2 automations switch on");
  });

  it("resumes on the step the person stopped on", () => {
    writeWizardProgress("f1", { step: "automations", settled: ["about", "autonomy"], skippedIntegrations: false });
    open();
    expect(screen.getByTestId("fleet-setup-wizard")).toHaveAttribute("data-step", "automations");
  });
});

describe("fleet-setup-wizard rules", () => {
  it("drops Integrations only when the fleet is known to have no slots", () => {
    expect(wizardSteps(false)).toEqual(["about", "autonomy", "automations", "review"]);
    expect(wizardSteps(true)).toContain("integrations");
    expect(wizardSteps(null)).toContain("integrations");
  });

  it("walks only the steps from startAt on when a parent flow walked the earlier ones (UX r1 B1)", () => {
    expect(wizardSteps(true, "automations")).toEqual(["automations", "review"]);
    expect(wizardSteps(false, "about")).toEqual(["about", "autonomy", "automations", "review"]);
  });

  it("opens on startAt, else the saved step still walked, else the first", () => {
    const steps = wizardSteps(false);
    expect(openingWizardStep(steps, null)).toBe("about");
    expect(openingWizardStep(steps, { step: "integrations", settled: [], skippedIntegrations: false })).toBe("about");
    expect(openingWizardStep(steps, { step: "review", settled: [], skippedIntegrations: false })).toBe("review");
    expect(openingWizardStep(steps, null, "automations")).toBe("automations");
  });

  it("ignores unreadable saved progress", () => {
    window.localStorage.setItem("xtm.fleet-setup.f2", "{not json");
    expect(readWizardProgress("f2")).toBeNull();
  });

  it("pickedPlan sends no ids when every row is picked and keeps the plan's module order", () => {
    const items = [
      { id: "a1", name: "A", providers: ["opencti", "openaev"], missing: ["openaev", "opencti"], selected: true },
      { id: "a2", name: "B", providers: ["opencrq"], missing: ["opencrq"], selected: true },
    ];
    expect(pickedPlan(items, new Set()).assignmentIds).toBeUndefined();
    expect(pickedPlan(items, new Set()).missing).toEqual(["openaev", "opencti", "opencrq"]);
    expect(pickedPlan(items, new Set(["a1"]))).toMatchObject({ assignmentIds: ["a2"], missing: ["opencrq"] });
    expect([...initiallyLeftOut(items)]).toEqual(["a1", "a2"]);
  });

  it("sums the load of a pick and prices it from the plan's mean run cost", () => {
    const items = [
      { id: "a1", name: "A", providers: [], missing: [], selected: true, runs_per_day: 0.3 },
      { id: "a2", name: "B", providers: [], missing: [], selected: true, runs_per_day: 4, event_driven: true },
    ];
    const estimate = { scheduled_runs_per_day: 4.3, event_automations: 1, avg_cost_per_run: 0.1, cost_per_day: 0.43, cost_days: 30 };
    expect(pickedLoad(items, estimate)).toEqual({ runs: 4.3, perMonth: false, eventDriven: 1, cost: 0.1 * 4.3, costDays: 30 });
    expect(pickedLoad([], null)).toEqual({ runs: 0, perMonth: false, eventDriven: 0, cost: null, costDays: 30 });
  });

  it("says a pick under one run a day per month, from the unrounded rates (UX round 1 B3 on #1550)", () => {
    const monthly = [
      { id: "m1", name: "Monthly report", providers: [], missing: [], selected: true, runs_per_day: 0.0333 },
      { id: "m2", name: "Half-year review", providers: [], missing: [], selected: true, runs_per_day: 0.0056 },
    ];
    const estimate = { scheduled_runs_per_day: 0.0389, event_automations: 0, avg_cost_per_run: 2, cost_per_day: 0.08, cost_days: 30 };
    const load = pickedLoad(monthly, estimate);
    expect(load).toMatchObject({ runs: 1, perMonth: true, eventDriven: 0 });
    expect(load.cost).toBeCloseTo(2 * 0.0389 * 30);
    expect(pickedLoad([{ ...monthly[0], runs_per_day: 0.1429 }], null)).toMatchObject({ runs: 4, perMonth: true });
  });

  it("counts the slots bound and still to bind", () => {
    expect(slotCounts([{ slots: [{ state: "filled" }, { state: "empty" }] }, { slots: [{ state: "unavailable" }] }])).toEqual({ bound: 1, total: 3, missing: 2 });
    expect(slotCounts(undefined)).toEqual({ bound: 0, total: 0, missing: 0 });
  });

  it("names a module by its product name", () => {
    expect(providerName("opencti")).toBe("OpenCTI");
    expect(providerName("other")).toBe("other");
  });
});
