// Fleets row of the CTEM command center v2 (C2 slice V6): fleets off is one
// closable invitation with Set up per fleet, a fleet on is one line with its
// opt-in state, last outcome and hours saved, never "Healthy" or a zero tile.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { FleetActivity, FleetActivityTile } from "@/lib/fleet-activity";
import { FleetsRowCard } from "./FleetsRowCard";

vi.mock("@/hooks/api/useFleetApi", () => ({
  useFleetControl: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useFleetDocumentation: (id: string) => ({
    data: id === "a" ? { structured: { quota: [{ trigger: "Weekly sweep", runs_min: 4, runs_max: 9 }] } } : { structured: null },
  }),
}));

const tile = (id: string, extra: Partial<FleetActivityTile> = {}): FleetActivityTile => ({
  id,
  name: `Fleet ${id}`,
  slug: `ctem-${id}`,
  description: "",
  optIn: "on",
  icon: "network",
  color: "blue",
  builtin: true,
  members: 2,
  status: "ok",
  runsInFlight: 0,
  recentRuns: 1,
  failures24h: 0,
  openWorkItems: 0,
  staleWorkItems: 0,
  lastRunAt: null,
  lastRunStatus: null,
  units: null,
  hoursSaved: null,
  netValue: null,
  currency: null,
  reviewRate: null,
  ...extra,
});

const activity = (tiles: FleetActivityTile[]): FleetActivity => ({
  tiles,
  totals: { runsInFlight: 0, recentRuns: 0, openWorkItems: 0, hoursSaved: null },
  roiAvailable: false,
  estimated: true,
  allOff: tiles.length > 0 && tiles.every((t) => t.optIn === "not_set_up"),
});

function Where() {
  return <p data-testid="where">{useLocation().pathname}</p>;
}

function card(value: FleetActivity, props: Partial<Parameters<typeof FleetsRowCard>[0]> = {}) {
  renderWithProviders(
    <MemoryRouter>
      <Routes>
        <Route
          path="*"
          element={
            <>
              <FleetsRowCard activity={value} isLoading={false} {...props} />
              <Where />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

const off = () => activity([tile("a", { optIn: "not_set_up", status: null }), tile("b", { optIn: "not_set_up", status: null })]);

describe("FleetsRowCard", () => {
  it("with every fleet off, invites an administrator with Set up per fleet and the documented runs", () => {
    card(off(), { canSetUp: true, onDismissInvite: () => {} });
    expect(screen.getByRole("heading", { name: "Add agentic work to this page" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Set up Fleet a" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Set up Fleet b" })).toBeTruthy();
    expect(screen.getByText("About 4 to 9 runs per month")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Close the fleets invitation" })).toBeTruthy();
    expect(screen.queryByText("Healthy")).toBeNull();
    expect(screen.queryByText(/h saved/)).toBeNull();
  });

  it("Set up only opens the fleet page", () => {
    card(off(), { canSetUp: true });
    fireEvent.click(screen.getByRole("button", { name: "Set up Fleet a" }));
    expect(screen.getByTestId("where").textContent).toBe("/fleets/a");
  });

  it("tells a non-administrator who sets fleets up, with no Set up", () => {
    card(off(), { canSetUp: false });
    expect(screen.getByText("Fleets are off: this page reads your connected modules only. An administrator can set them up.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Set up/ })).toBeNull();
  });

  it("once closed, keeps one quiet line", () => {
    card(off(), { canSetUp: true, inviteShown: false });
    expect(screen.queryByRole("heading", { name: "Add agentic work to this page" })).toBeNull();
    expect(screen.getByTestId("ctem-fleet-invite-closed")).toBeTruthy();
  });

  it("draws a fleet on as a tile and a fleet off as an invitation row under it, with no ROI figure (#11996, #10721)", () => {
    card(
      activity([
        tile("a", { hoursSaved: 11, lastRunAt: new Date().toISOString(), lastRunStatus: "completed" }),
        tile("b", { optIn: "not_set_up", status: null }),
      ]),
      { canSetUp: true },
    );
    expect(screen.getAllByTestId("fleet-tile")).toHaveLength(1);
    expect(screen.getAllByTestId("ctem-fleet-invite-row")).toHaveLength(1);
    expect(screen.queryByText(/h saved/)).toBeNull();
    expect(screen.getByRole("button", { name: "Set up Fleet b" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Set up Fleet a" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open fleet Fleet a" }));
    expect(screen.getByTestId("where").textContent).toBe("/fleets/a");
  });

  it("fills a tile with its health, KPIs, 7-day stacked bars and last run summary (#11996)", () => {
    card(
      activity([
        tile("a", {
          automationsOn: 5,
          automationsTotal: 8,
          offAutomations: ["Weekly access review"],
          openWorkItems: 3,
          succeededByDay: [1, 2, 0, 0, 3, 0, 4],
          failedByDay: [0, 1, 0, 0, 0, 0, 0],
          lastRunAt: new Date().toISOString(),
          lastRunStatus: "completed",
          lastSummary: "Closed 4 stale work items",
        }),
      ]),
    );
    expect(screen.getByTestId("fleet-tile-health").textContent).toContain("Healthy");
    expect(screen.getByTestId("fleet-tile-automations").textContent).toContain("5/8");
    expect(screen.getByTestId("fleet-tile-success").textContent).toContain("91%");
    expect(screen.getByTestId("fleet-tile-open").textContent).toContain("3");
    expect(screen.getAllByTestId("fleet-tile-bar")).toHaveLength(7);
    expect(screen.getByRole("img", { name: "Today: 4 succeeded, 0 failed" })).toBeTruthy();
    expect(screen.getByTestId("fleet-tile-runs").textContent).toBe("11 runs · 1 failed");
    expect(screen.getByTestId("fleet-tile-summary").textContent).toBe("Closed 4 stale work items");
    expect(screen.getByTestId("ctem-fleets-row-summary").textContent).toBe("1 fleet · 11 runs in 7 days · 91% succeeded");
  });

  it("says No runs without a rate, and the failing count as a link to the fleet (#11996)", () => {
    card(
      activity([
        tile("a", { status: "failing", failingMembers: 2, memberCount: 5, succeededByDay: [0, 0, 0, 0, 0, 0, 0], failedByDay: [0, 0, 0, 0, 0, 0, 0] }),
      ]),
    );
    expect(screen.getByTestId("fleet-tile-success").textContent).toContain("No runs");
    fireEvent.click(screen.getByRole("button", { name: "2 of 5 agents failing" }));
    expect(screen.getByTestId("where").textContent).toBe("/fleets/a");
  });

  it("draws 4 fleets on as one tile each (UX round 1 B5 on #12015)", () => {
    card(activity(["a", "b", "c", "d"].map((id) => tile(id))));
    expect(screen.getAllByTestId("fleet-tile")).toHaveLength(4);
    expect(screen.getByTestId("ctem-fleets-row-summary").textContent).toContain("4 fleets");
  });

  it("gives every tile one child per subgrid row, so a wrapping value keeps its row aligned (UX round 2 B1 on #12015)", () => {
    card(activity([tile("a", { succeededByDay: [0, 0, 0, 0, 0, 0, 0], failedByDay: [0, 0, 0, 0, 0, 0, 0] }), tile("b", { succeededByDay: [1, 1, 1, 1, 1, 1, 1], failedByDay: [0, 0, 0, 0, 0, 0, 0] })]));
    const tiles = screen.getAllByTestId("fleet-tile");
    expect(tiles).toHaveLength(2);
    for (const t of tiles) expect(t.children).toHaveLength(5);
    expect(screen.getAllByTestId("fleet-tile-success")[0].textContent).toContain("No runs");
  });

  it("draws 5 fleets on as one tile each (UX round 1 B5 on #12015)", () => {
    card(activity(["a", "b", "c", "d", "e"].map((id) => tile(id))));
    expect(screen.getAllByTestId("fleet-tile")).toHaveLength(5);
    expect(screen.getByRole("button", { name: "Open fleet Fleet e" })).toBeTruthy();
  });

  it("marks a paused fleet with its chip and offers Resume to an administrator (#11996)", () => {
    card(activity([tile("a"), tile("b", { optIn: "paused", status: null })]), { canSetUp: true });
    expect(screen.getByTestId("ctem-fleet-invite-paused")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Resume Fleet b" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Set up Fleet b" })).toBeNull();
  });

  it("with no fleet at all, draws the empty state with Create for an administrator only (#11996)", () => {
    card(activity([]), { canSetUp: true, noFleet: true, onChooseFleets: () => {} });
    expect(screen.getByTestId("ctem-fleets-row-empty")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Create a fleet/ })).toBeTruthy();
  });

  it("with no fleet at all, tells a non-administrator who creates one (#11996)", () => {
    card(activity([]), { canSetUp: false, noFleet: true });
    expect(screen.getByText("A fleet is a team of agents around an orchestrator. Ask an administrator to create one.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Create a fleet/ })).toBeNull();
  });

  it("in Community Edition says fleets need Enterprise Edition, with no fleet link", () => {
    card(off(), { licensed: false, lockReason: "unlicensed" });
    expect(screen.getByTestId("ctem-fleets-row-ce")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /View all fleets/ })).toBeNull();
  });

  it("offers Choose fleets and a hide control on every row (#11464)", () => {
    const onHideFleet = vi.fn();
    const onChooseFleets = vi.fn();
    card(activity([tile("a"), tile("b", { optIn: "not_set_up", status: null })]), { canSetUp: true, onHideFleet, onChooseFleets });
    fireEvent.click(screen.getByRole("button", { name: "Hide Fleet a from this page" }));
    expect(onHideFleet).toHaveBeenCalledWith(expect.objectContaining({ id: "a" }));
    fireEvent.click(screen.getByTestId("ctem-choose-fleets-open"));
    expect(onChooseFleets).toHaveBeenCalled();
  });

  it("lets a built-in fleet be hidden from the invitation too (#11464)", () => {
    const onHideFleet = vi.fn();
    card(off(), { canSetUp: true, onHideFleet, onChooseFleets: () => {} });
    fireEvent.click(screen.getByRole("button", { name: "Hide Fleet b from this page" }));
    expect(onHideFleet).toHaveBeenCalledWith(expect.objectContaining({ id: "b" }));
  });

  it("with every fleet hidden, says so with the way back (#11464)", () => {
    card(activity([]), { canSetUp: true, onChooseFleets: () => {} });
    expect(screen.getByTestId("ctem-fleets-row-none")).toBeTruthy();
    expect(screen.getByTestId("ctem-choose-fleets-open")).toBeTruthy();
  });

  it("reports the height of its content", () => {
    const onContentHeight = vi.fn();
    card(off(), { canSetUp: true, onContentHeight });
    expect(onContentHeight).toHaveBeenCalled();
  });
});
