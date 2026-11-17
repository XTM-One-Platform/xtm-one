// Fleets widget of the Copilot home (#12063, UX design rounds 1 and 2): the
// shared tile, one grid row with a capped summary, attention order by default,
// the empty, hidden, all-off and Community Edition states, no CTEM wording.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import { byAttention, type FleetActivity, type FleetActivityTile } from "@/lib/fleet-activity";
import { CopilotFleetsCard, shownTiles, tilesPerRow } from "./CopilotFleetsCard";

vi.mock("@/hooks/api/useFleetApi", () => ({
  useFleetControl: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useFleetDocumentation: () => ({ data: { structured: null } }),
}));

const week = (n: number) => [0, 0, 0, 0, 0, 0, n];

const tile = (id: string, extra: Partial<FleetActivityTile> = {}): FleetActivityTile => ({
  id,
  name: `Fleet ${id}`,
  slug: `fleet-${id}`,
  description: "",
  optIn: "on",
  icon: "network",
  color: "blue",
  builtin: false,
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
  automationsOn: 2,
  automationsTotal: 2,
  succeededByDay: week(1),
  failedByDay: week(0),
  failingMembers: 0,
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

function card(value: FleetActivity, props: Partial<Parameters<typeof CopilotFleetsCard>[0]> = {}) {
  renderWithProviders(
    <MemoryRouter>
      <Routes>
        <Route
          path="*"
          element={
            <>
              <CopilotFleetsCard activity={value} isLoading={false} onChooseFleets={() => {}} {...props} />
              <Where />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe("byAttention (round 2 B3)", () => {
  it("puts a failing fleet first, then stale work items, then automations off, then the most runs", () => {
    const tiles = [
      tile("busy", { succeededByDay: week(90) }),
      tile("quiet", { succeededByDay: week(2) }),
      tile("off", { automationsOn: 1, automationsTotal: 3 }),
      tile("stale", { staleWorkItems: 4 }),
      tile("failing", { failingMembers: 1 }),
    ];
    expect(byAttention(tiles).map((t) => t.id)).toEqual(["failing", "stale", "off", "busy", "quiet"]);
  });
});

describe("shownTiles and tilesPerRow (round 2 B2)", () => {
  it("holds 2 tiles below 1000 px and 3 from 1000 px", () => {
    expect(tilesPerRow(null)).toBe(2);
    expect(tilesPerRow(999)).toBe(2);
    expect(tilesPerRow(1000)).toBe(3);
  });

  it("keeps the person's order when they chose, and never draws a fleet that is off", () => {
    const tiles = [tile("b", { succeededByDay: week(1) }), tile("a", { failingMembers: 1 }), tile("c", { optIn: "not_set_up" })];
    expect(shownTiles(tiles, true, 3).map((t) => t.id)).toEqual(["b", "a"]);
    expect(shownTiles(tiles, false, 3).map((t) => t.id)).toEqual(["a", "b"]);
  });
});

describe("CopilotFleetsCard", () => {
  it("draws one fleet as one shared tile, with its summary", () => {
    card(activity([tile("a", { succeededByDay: week(4) })]));
    expect(screen.getAllByTestId("fleet-tile")).toHaveLength(1);
    expect(screen.getByTestId("copilot-fleets-summary").textContent).toContain("1 fleet");
    expect(screen.getByTestId("copilot-fleets-summary").textContent).toContain("4 runs in 7 days");
  });

  it("caps the row and counts runs over the tiles shown: 2 of 3 fleets (unmeasured width = 2 tiles)", () => {
    card(activity([tile("a", { succeededByDay: week(5) }), tile("b", { succeededByDay: week(3) }), tile("c", { succeededByDay: week(100) })]));
    expect(screen.getAllByTestId("fleet-tile")).toHaveLength(2);
    const summary = screen.getByTestId("copilot-fleets-summary").textContent ?? "";
    expect(summary).toContain("2 of 3 fleets");
    expect(summary).toContain("105 runs in 7 days");
  });

  it("with no fleet, says what a fleet is and offers Create a fleet to an administrator, no header link", () => {
    card(activity([]), { noFleet: true, canSetUp: true });
    expect(screen.getByText("No fleet yet")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Create a fleet/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /View all fleets/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Choose fleets/ })).toBeNull();
  });

  it("with no fleet and no right, offers Open Fleets", () => {
    card(activity([]), { noFleet: true, canSetUp: false });
    fireEvent.click(screen.getByRole("button", { name: /Open Fleets/ }));
    expect(screen.getByTestId("where").textContent).toBe("/fleets");
    expect(screen.queryByRole("button", { name: /Create a fleet/ })).toBeNull();
  });

  it("with every fleet hidden, gives the way back", () => {
    card(activity([]), { noFleet: false });
    expect(screen.getByTestId("copilot-fleets-none")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Choose fleets/ })).toBeTruthy();
  });

  it("with every fleet off, a neutral sentence and Set up per fleet, no CTEM invitation", () => {
    card(activity([tile("a", { optIn: "not_set_up", status: null }), tile("b", { optIn: "not_set_up", status: null })]), { canSetUp: true });
    expect(screen.getByText("Your fleets are ready to set up")).toBeTruthy();
    expect(screen.getByText("None of the 2 fleets runs yet. Set one up to see its activity here.")).toBeTruthy();
    expect(screen.queryByText("Add agentic work to this page")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Set up Fleet a" }));
    expect(screen.getByTestId("where").textContent).toBe("/fleets/a");
  });

  it("in Community Edition, the lock sentence and no header link", () => {
    card(activity([]), { licensed: false });
    expect(screen.getByText("Fleets run with Enterprise Edition")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /View all fleets/ })).toBeNull();
  });

  it("carries no CTEM or security wording in any state", () => {
    card(activity([tile("a"), tile("b", { optIn: "not_set_up", status: null })]), { canSetUp: true });
    expect(document.body.textContent ?? "").not.toMatch(/CTEM|exposure|threat|cyber|command center/i);
  });
});
