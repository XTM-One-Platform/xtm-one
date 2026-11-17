// Enable fleet, the setup dialog (#9769 S4b, UX #9835 N1): it shows what the
// plan switches on, keeps Confirm disabled while a module is missing, never
// enables anything on opening, and enables in one call on Confirm.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { FleetEnableResult } from "@/hooks/api/useFleetApi";
import { renderWithProviders } from "@/testing/providers";

import { FleetEnableDialog, pickedPlan, providerName } from "./FleetEnableDialog";

const api = vi.hoisted(() => ({
  plan: undefined as FleetEnableResult | undefined,
  quota: [{ trigger: "New threat", runs_min: 2, runs_max: 16 }] as { trigger: string; runs_min: number; runs_max: number }[] | undefined,
  mutateAsync: vi.fn(async () => ({ changed: 2 })),
  aiOff: false,
}));
vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => api.aiOff,
  useAiDisabledReason: () => (api.aiOff ? "AI is disabled" : null),
}));
vi.mock("@/hooks/api/useFleetApi", () => ({
  useFleetEnablePlan: () => ({ data: api.plan, error: null }),
  useFleetDocumentation: () => ({ data: { structured: { quota: api.quota } }, isError: false }),
  useFleetEnable: () => ({ mutateAsync: api.mutateAsync, isPending: false }),
}));
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

beforeEach(() => {
  api.plan = plan();
  api.quota = [{ trigger: "New threat", runs_min: 2, runs_max: 16 }];
  api.aiOff = false;
  api.mutateAsync.mockClear();
});
afterEach(cleanup);

const open = () =>
  renderWithProviders(
    <MemoryRouter>
      <FleetEnableDialog fleetId="f1" fleetName="AI security" open onOpenChange={() => undefined} />
    </MemoryRouter>,
  );

describe("FleetEnableDialog", () => {
  it("lists what switches on, the run-as user and the run estimate, and writes nothing on opening", () => {
    open();
    expect(screen.getByRole("dialog", { name: "Enable AI security?" })).toBeInTheDocument();
    expect(screen.getByText("Daily exposure review")).toBeInTheDocument();
    expect(screen.getByText("Weekly simulation")).toBeInTheDocument();
    // B1: the run-as value is a labelled field tied to its helper line.
    const runAs = screen.getByRole("textbox", { name: "Run as" });
    expect(runAs).toHaveValue("You (Ana)");
    expect(runAs).toHaveAccessibleDescription("Its automations use this user's connected modules, model and memory.");
    // B3: the same basis as About > Quota estimate.
    expect(screen.getByTestId("fleet-enable-estimate")).toHaveTextContent("2 to 16 runs a month, from the fleet's documented triggers");
    expect(api.mutateAsync).not.toHaveBeenCalled();
  });

  it("enables in one call on Confirm, as the caller", async () => {
    open();
    fireEvent.click(screen.getByRole("button", { name: "Enable 2 automations" }));
    await waitFor(() => expect(api.mutateAsync).toHaveBeenCalledTimes(1));
    // Every row picked: the call names no subset, the server picks every one.
    expect(api.mutateAsync).toHaveBeenCalledWith({ runAs: null, assignmentIds: undefined });
  });

  it("enables only the picked automations when one is left out (#9173 S5)", async () => {
    open();
    const rows = screen.getAllByRole("checkbox");
    // Select all + one box per automation, every one checked on opening.
    expect(rows).toHaveLength(3);
    rows.forEach((box) => expect(box).toHaveAttribute("aria-checked", "true"));
    fireEvent.click(screen.getByRole("checkbox", { name: "Switch on Weekly simulation" }));
    expect(screen.getByRole("checkbox", { name: "Select every automation that is off" })).toHaveAttribute("aria-checked", "mixed");
    fireEvent.click(screen.getByRole("button", { name: "Enable 1 automation" }));
    await waitFor(() => expect(api.mutateAsync).toHaveBeenCalledTimes(1));
    expect(api.mutateAsync).toHaveBeenCalledWith({ runAs: null, assignmentIds: ["a1"] });
  });

  it("keeps Confirm disabled with nothing picked", () => {
    open();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select every automation that is off" }));
    expect(screen.getByText("Pick at least one automation to switch on.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enable fleet" })).toBeDisabled();
  });

  it("leaving out the automation that misses a module unblocks Confirm", () => {
    api.plan = plan({
      ready: false,
      missing: ["openaev"],
      items: [
        { id: "a1", name: "Daily exposure review", providers: ["opencti"], missing: [], selected: true },
        { id: "a2", name: "Weekly simulation", providers: ["openaev"], missing: ["openaev"], selected: true },
      ],
    });
    open();
    expect(screen.getByRole("button", { name: "Enable 2 automations" })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: "Switch on Weekly simulation" }));
    expect(screen.queryByTestId("fleet-enable-missing")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enable 1 automation" })).toBeEnabled();
  });

  it("marks the row that misses a module and names it in the notice (#10250 UX r1 B1)", () => {
    api.plan = plan({
      ready: false,
      missing: ["openaev"],
      items: [
        { id: "a1", name: "Daily exposure review", providers: ["opencti"], missing: [], selected: true },
        { id: "a2", name: "Weekly simulation", providers: ["openaev"], missing: ["openaev"], selected: true },
      ],
    });
    open();
    const rows = screen.getByTestId("fleet-enable-items").querySelectorAll("li");
    expect(rows[0]).toHaveAttribute("data-missing", "false");
    expect(rows[0].querySelector("[data-testid='fleet-enable-item-missing']")).toBeNull();
    expect(rows[1]).toHaveAttribute("data-missing", "true");
    expect(rows[1].querySelector("[data-testid='fleet-enable-item-missing']")).toHaveTextContent("Needs OpenAEV");
    expect(rows[1].querySelector(".opacity-40")).not.toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("Weekly simulation needs OpenAEV.");
    expect(screen.getByRole("alert")).toHaveTextContent("Leave it out to switch on the others.");
  });

  it("pickedPlan sends no ids when every row is picked and keeps the plan's module order", () => {
    const items = [
      { id: "a1", name: "A", providers: ["opencti", "openaev"], missing: ["openaev", "opencti"], selected: true },
      { id: "a2", name: "B", providers: ["opencrq"], missing: ["opencrq"], selected: true },
    ];
    expect(pickedPlan(items, new Set()).assignmentIds).toBeUndefined();
    expect(pickedPlan(items, new Set()).missing).toEqual(["openaev", "opencti", "opencrq"]);
    expect(pickedPlan(items, new Set(["a1"]))).toMatchObject({ assignmentIds: ["a2"], missing: ["opencrq"] });
  });

  it("keeps Confirm disabled and names the module while one is missing", () => {
    api.plan = plan({
      ready: false,
      missing: ["openaev"],
      items: [
        { id: "a1", name: "Daily exposure review", providers: ["opencti"], missing: [], selected: true },
        { id: "a2", name: "Weekly simulation", providers: ["openaev"], missing: ["openaev"], selected: true },
      ],
    });
    open();
    expect(screen.getByRole("alert")).toHaveTextContent("You have not connected OpenAEV. Connect it in Integrations");
    expect(screen.getByRole("link", { name: "Open Integrations" })).toHaveAttribute("href", "/integrations");
    expect(screen.getByRole("button", { name: "Enable 2 automations" })).toBeDisabled();
  });

  it("names several missing modules in the plural", () => {
    api.plan = plan({
      ready: false,
      missing: ["opencti", "openaev"],
      items: [
        { id: "a1", name: "Daily exposure review", providers: ["opencti"], missing: ["opencti"], selected: true },
        { id: "a2", name: "Weekly simulation", providers: ["openaev"], missing: ["openaev"], selected: true },
      ],
    });
    open();
    expect(screen.getByRole("alert")).toHaveTextContent("You have not connected OpenCTI, OpenAEV. Connect them in Integrations");
  });

  it("says when no estimate is written for the fleet", () => {
    api.quota = undefined;
    open();
    expect(screen.getByTestId("fleet-enable-estimate")).toHaveTextContent("No estimate written for this fleet");
  });

  it("says nothing is off and offers no count when every automation is on", () => {
    api.plan = plan({ changed: 0, ready: false, items: [] });
    open();
    expect(screen.getByText("Nothing to enable: every automation of this fleet is on.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enable fleet" })).toBeDisabled();
  });

  it("keeps Confirm disabled while AI is off (the enable endpoint is AI gated)", () => {
    api.aiOff = true;
    open();
    expect(screen.getByRole("button", { name: "Enable 2 automations" })).toBeDisabled();
  });

  it("names a module by its product name", () => {
    expect(providerName("opencti")).toBe("OpenCTI");
    expect(providerName("other")).toBe("other");
  });
});
