// The fleet autonomy strip (#9769 S3a, UX round 1 on #9887): focus and arrow
// keys never commit a level (B1), the group is named "Human approval" (B2),
// each step's tip shows on keyboard focus with this fleet's counts (B3), and a
// fleet with no declared gate says so (B5). Truthful control (#10025, slice B
// of #10000): no level selected without a catalog, the actions by name, the
// inline explainer, the direction named by its effect.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";

import type { FleetAutonomyGate } from "@/hooks/api/useFleetApi";
import { renderWithProviders } from "@/testing/providers";

import { FleetAutonomyStrip } from "./FleetAutonomyStrip";

const api = vi.hoisted(() => ({
  catalog: [] as FleetAutonomyGate[],
  gates: undefined as { stored: boolean; editable?: boolean; instructions_position?: string | null } | undefined,
  mutateAsync: vi.fn(async () => undefined),
}));
vi.mock("@/hooks/api/useFleetApi", () => ({
  useFleetDocumentation: () => ({ data: { autonomy_level: "medium", autonomy_catalog: api.catalog } }),
  useSetFleetAutonomy: () => ({ mutateAsync: api.mutateAsync, isPending: false }),
  useSetFleetGatePins: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useFleetApprovalGates: () => ({ data: api.gates, isLoading: false, isError: false, refetch: vi.fn() }),
  useSetFleetApprovalGates: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useSetFleetInstructionsPosition: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

// The pending approvals section (#10202) has its own suite: nothing waits here.
vi.mock("./FleetPendingApprovals", () => ({ FleetPendingApprovals: () => null }));

const toasts = vi.hoisted(() => ({ calls: [] as { title?: string; description?: string; action?: { onClick: () => void } }[] }));
vi.mock("@/components/ui/toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/ui/toast")>()),
  useToast: () => ({ toast: (t: (typeof toasts.calls)[number]) => toasts.calls.push(t) }),
}));

const gate = (action: string, medium: boolean): FleetAutonomyGate => ({
  key: action,
  action,
  module: "platform",
  impact: medium ? "high" : "low",
  gated: true,
  by_level: { full_gates: true, medium, low: false, autonomous: false },
});

const nextFrame = () => act(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));

beforeEach(() => {
  api.catalog = [gate("Create a ticket", false), gate("Launch a scenario", true)];
  api.gates = undefined;
  api.mutateAsync.mockClear();
  toasts.calls = [];
});
afterEach(cleanup);

describe("FleetAutonomyStrip", () => {
  it("is a group named Human approval, its steps toggle buttons that arrow keys never commit", () => {
    renderWithProviders(<FleetAutonomyStrip fleetId="f1" level="medium" canManage isAdmin />);
    const group = screen.getByRole("group", { name: "Human approval" });
    expect(screen.getByText("Human approval", { selector: "span" })).toBeInTheDocument();
    expect(screen.queryByRole("radio")).toBeNull();

    const medium = screen.getByRole("button", { name: "Medium (autonomy level)" });
    expect(medium).toHaveAttribute("aria-pressed", "true");
    act(() => medium.focus());
    for (const key of ["ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp", "Home", "End"]) fireEvent.keyDown(medium, { key });
    expect(api.mutateAsync).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(group).toBeInTheDocument();
  });

  it("lowers on a click, with no dialog", () => {
    renderWithProviders(<FleetAutonomyStrip fleetId="f1" level="medium" canManage isAdmin />);
    fireEvent.click(screen.getByRole("button", { name: "Full human gates" }));
    expect(api.mutateAsync).toHaveBeenCalledWith("full_gates");
  });

  it("words Undo from the level it leaves: a reduction naming the actions that run alone (UX round 4 B2 on #10034)", async () => {
    renderWithProviders(<FleetAutonomyStrip fleetId="f1" level="medium" canManage isAdmin />);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Full human gates" })));
    const raised = toasts.calls[toasts.calls.length - 1];
    expect(raised?.title).toMatch(/increased to/);
    expect(raised?.description).toMatch(/1 action now waits for approval/);
    // The toast's Undo runs from a render where the level prop is still the old one.
    await act(async () => raised?.action?.onClick());
    expect(api.mutateAsync).toHaveBeenLastCalledWith("medium");
    const undone = toasts.calls[toasts.calls.length - 1];
    expect(undone?.title).toMatch(/reduced to/);
    expect(undone?.description).toBe("1 action now runs without approval");
    expect(undone?.action).toBeUndefined();
  });

  it("shows a step's meaning and this fleet's counts on keyboard focus", async () => {
    renderWithProviders(<FleetAutonomyStrip fleetId="f1" level="medium" canManage isAdmin />);
    const low = screen.getByRole("button", { name: "Low (autonomy level)" });
    expect(low.getAttribute("title")).toBeNull();
    act(() => low.focus());
    await nextFrame();
    const tip = await screen.findByRole("tooltip");
    expect(tip).toHaveTextContent("Only destructive actions and actions on production assets wait.");
    // The approved wording, waits first (UX round 1 B3 on #10034).
    expect(tip).toHaveTextContent("0 actions wait for approval, 2 run alone");
  });

  it("names, before the click, the actions a step changes in both directions (UX round 2 B4 on #10034)", async () => {
    renderWithProviders(<FleetAutonomyStrip fleetId="f1" level="medium" canManage isAdmin />);
    const full = screen.getByRole("button", { name: "Full human gates" });
    act(() => full.focus());
    await nextFrame();
    expect(await screen.findByTestId("fleet-autonomy-change-full_gates")).toHaveTextContent(
      "Add approval back: 1 action starts waiting: Create a ticket",
    );
    act(() => full.blur());
    const low = screen.getByRole("button", { name: "Low (autonomy level)" });
    act(() => low.focus());
    await nextFrame();
    expect(await screen.findByTestId("fleet-autonomy-change-low")).toHaveTextContent(
      "Reduce approval: 1 action runs alone: Launch a scenario",
    );
  });

  it("summarises the current level in the approved wording", () => {
    renderWithProviders(<FleetAutonomyStrip fleetId="f1" level="medium" canManage isAdmin />);
    expect(screen.getByTestId("fleet-autonomy-counts")).toHaveTextContent("1 action waits for approval, 1 runs alone");
  });

  it("is read only for a reader who does not manage the fleet, with one line saying why", () => {
    // Same named lists for a reader (UX round 3 B3).
    renderWithProviders(<FleetAutonomyStrip fleetId="f1" level="medium" canManage={false} isAdmin={false} />);
    const medium = screen.getByRole("button", { name: "Medium (autonomy level)" });
    expect(medium).toBeDisabled();
    expect(medium).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("fleet-autonomy-read-only")).toHaveTextContent("Read only: you do not manage this fleet.");
  });

  it("names the reduction by its effect and lists one line per action that will run without approval", async () => {
    renderWithProviders(<FleetAutonomyStrip fleetId="f1" level="medium" canManage isAdmin />);
    fireEvent.click(screen.getByRole("button", { name: "Low (autonomy level)" }));
    expect(await screen.findByText("Reduce human approval to Low (autonomy level)?")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reduce to Low (autonomy level)" })).toBeInTheDocument();
    const list = await screen.findByTestId("fleet-autonomy-unlocked");
    expect(list.querySelectorAll("li")).toHaveLength(1);
    expect(list).toHaveTextContent("Launch a scenario");
    expect(api.mutateAsync).not.toHaveBeenCalled();
  });

  it("puts the enforced row and the instructions row in one sources grid, label then control (UX r1 B2 of #10178)", () => {
    api.gates = { stored: false, editable: true, instructions_position: "medium_low" };
    renderWithProviders(<FleetAutonomyStrip fleetId="f1" level="medium" canManage isAdmin />);
    const sources = screen.getByTestId("fleet-autonomy-sources");
    expect(sources).toContainElement(screen.getByTestId("fleet-autonomy-enforced-label"));
    expect(sources).toContainElement(screen.getByTestId("fleet-autonomy-levels"));
    expect(sources).toContainElement(screen.getByTestId("fleet-instructions-position-label"));
    expect(sources).toContainElement(screen.getByTestId("fleet-instructions-position-select"));
  });

  it("shows the instructions-governed position, never enforced, and lets an administrator move it", () => {
    api.gates = { stored: false, editable: true, instructions_position: "medium_low" };
    renderWithProviders(<FleetAutonomyStrip fleetId="f1" level="medium" canManage isAdmin />);
    const line = screen.getByTestId("fleet-instructions-position");
    expect(line).toHaveTextContent("Set by the fleet's instructions, not enforced by the platform");
    expect(line).toHaveTextContent("Between Medium and Low");
    expect(screen.getByRole("combobox", { name: "Instructions position" })).toBeInTheDocument();
  });

  it("shows the instructions position read only, and Not set when none is stored", () => {
    api.gates = { stored: false, editable: false, instructions_position: null };
    renderWithProviders(<FleetAutonomyStrip fleetId="f1" level="medium" canManage={false} isAdmin={false} />);
    expect(screen.getByTestId("fleet-instructions-position-value")).toHaveTextContent("Not set");
    expect(screen.queryByRole("combobox", { name: "Instructions position" })).toBeNull();
  });

  it("shows the matrix of every action with no click, no grouped lists and no toggle, and what the level does not cover (#1493)", () => {
    renderWithProviders(<FleetAutonomyStrip fleetId="f1" level="medium" canManage isAdmin />);
    const matrix = screen.getByTestId("about-autonomy-catalog");
    expect(matrix).toHaveTextContent("Launch a scenario");
    expect(matrix).toHaveTextContent("Create a ticket");
    expect(screen.queryByTestId("fleet-autonomy-waits")).toBeNull();
    expect(screen.queryByTestId("fleet-autonomy-runs-alone")).toBeNull();
    expect(screen.queryByTestId("fleet-autonomy-matrix-toggle")).toBeNull();
    expect(screen.queryByRole("button", { name: "Show what waits at each level" })).toBeNull();
    expect(screen.getByTestId("fleet-autonomy-not-covered")).toHaveTextContent("chat, channel and consult runs");
  });

  it("opens the explainer inline with headed parts, never repeating the not-covered line, and closes on Close and Esc (B4, C2)", () => {
    renderWithProviders(<FleetAutonomyStrip fleetId="f1" level="medium" canManage isAdmin />);
    const toggle = screen.getByRole("button", { name: "How human approval works" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const explainer = screen.getByTestId("fleet-autonomy-explainer");
    expect(screen.getByRole("heading", { name: "How human approval works" })).toBeInTheDocument();
    for (const term of ["What is gated", "Where it applies", "Who may change it", "What happens to a paused call"]) {
      expect(explainer).toHaveTextContent(term);
    }
    expect(explainer).not.toHaveTextContent(screen.getByTestId("fleet-autonomy-not-covered").textContent ?? "");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByTestId("fleet-autonomy-explainer")).toBeNull();
    expect(toggle).toHaveFocus();
    fireEvent.click(toggle);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByTestId("fleet-autonomy-explainer")).toBeNull();
  });

  it("keeps the compact setup strip free of the matrix", () => {
    renderWithProviders(<FleetAutonomyStrip fleetId="f1" level="medium" canManage isAdmin compact />);
    expect(screen.queryByTestId("about-autonomy-catalog")).toBeNull();
  });

  it("says under the matrix where the catalog comes from, with no PR number (C3)", () => {
    api.gates = { stored: true };
    renderWithProviders(<FleetAutonomyStrip fleetId="f1" level="medium" canManage isAdmin />);
    const source = screen.getByTestId("about-autonomy-source");
    expect(source).toHaveTextContent("stored in its configuration");
    expect(source.textContent).not.toMatch(/#\d/);
  });

  it("selects no level when the fleet has no catalog, and offers Review actions to a manager", () => {
    api.catalog = [];
    renderWithProviders(<FleetAutonomyStrip fleetId="f1" level="full_gates" canManage isAdmin />);
    expect(screen.queryByRole("group", { name: "Human approval" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Full human gates" })).toBeNull();
    expect(screen.getByTestId("fleet-autonomy-counts")).toHaveTextContent("No action of this fleet is gated by this control yet.");
    // Where approvals still apply is the "i" beside the line, never a second line under the band (#10215 slice b).
    expect(screen.getByRole("img", { name: /agent instructions and agent approval settings/ })).toBeInTheDocument();
    expect(screen.queryByText(/Approvals still apply where/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Review actions" }));
    expect(screen.getByTestId("fleet-gate-review")).toBeInTheDocument();
  });

  it("offers no Review actions to a reader who does not manage the fleet", () => {
    api.catalog = [];
    renderWithProviders(<FleetAutonomyStrip fleetId="f1" level="full_gates" canManage={false} isAdmin={false} />);
    expect(screen.queryByRole("button", { name: "Review actions" })).toBeNull();
  });
});
