// A figure the reader cannot read is a dash a keyboard can reach and a screen
// reader can explain (#9133 A3), never a bare unfocusable dash.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { FleetActivity, FleetActivityTile } from "@/lib/fleet-activity";
import { FleetActivityCard } from "./FleetActivityCard";
import type { AiLockReason } from "@/hooks/useAiLockReason";

const viewer = vi.hoisted(() => ({ isAdmin: true }));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: viewer.isAdmin }) }));
// The invitation reads each fleet's documented runs per month (UX B2 on #9570).
vi.mock("@/hooks/api/useFleetApi", () => ({
  useFleetDocumentation: (id: string) => ({
    data:
      id === "a"
        ? { structured: { quota: [{ trigger: "Weekly sweep", runs_min: 4, runs_max: 5 }, { trigger: "New threat", runs_min: 4, runs_max: 30 }] } }
        : { structured: null },
  }),
}));

const tile = (id: string, extra: Partial<FleetActivityTile> = {}): FleetActivityTile => ({
  id,
  name: `Fleet ${id}`,
  slug: `ctem-${id}`,
  description: `What fleet ${id} adds`,
  optIn: "on",
  icon: "network",
  color: "blue",
  builtin: false,
  members: 2,
  status: "ok",
  runsInFlight: 1,
  recentRuns: 3,
  failures24h: 0,
  openWorkItems: 2,
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
  totals: { runsInFlight: 1, recentRuns: 3, openWorkItems: 2, hoursSaved: null },
  roiAvailable: false,
  estimated: true,
  allOff: tiles.length > 0 && tiles.every((t) => t.optIn === "not_set_up"),
});

function renderCard(value: FleetActivity, props: Partial<Parameters<typeof FleetActivityCard>[0]> = {}) {
  return renderWithProviders(
    <MemoryRouter>
      <FleetActivityCard activity={value} isLoading={false} {...props} />
    </MemoryRouter>,
  );
}

describe("FleetActivityCard unreadable figures", () => {
  it("gives each dash its reason as text and a keyboard focus stop", () => {
    renderCard(activity([tile("a")]));
    const reasons = screen.getAllByText("ROI figures are readable by administrators");
    // Units, hours saved and net value are not readable without the ROI report.
    expect(reasons).toHaveLength(3);
    for (const reason of reasons) {
      const stop = reason.parentElement as HTMLElement;
      expect(stop.tabIndex).toBe(0);
      stop.focus();
      expect(document.activeElement).toBe(stop);
    }
  });

  it("says the review rate of a fleet whose ROI is readable, and nothing when it is not (#9807)", () => {
    renderCard(activity([tile("a", { units: 4, hoursSaved: 2, netValue: 100, currency: "USD", reviewRate: 0.5 }), tile("b")]));
    const lines = screen.getAllByTestId("fleet-tile-review-rate");
    expect(lines).toHaveLength(1);
    expect(lines[0].textContent).toContain("50");
  });

  it("links a review rate below 100 % to the fleet's unreviewed runs, and a full one to nothing (#9837 A2)", () => {
    renderCard(activity([tile("a", { reviewRate: 0 }), tile("b", { reviewRate: 1 })]));
    const links = screen.getAllByTestId("fleet-tile-review-link");
    expect(links).toHaveLength(1);
    // The ROI records of the tile's window, runs and work items alike (#10570).
    expect(links[0].getAttribute("href")).toBe("/fleets/a/roi/records?review=unreviewed&window=7d");
  });

  it("shows a readable figure as its value, with no reason", () => {
    renderCard(activity([tile("a")]));
    expect(screen.queryByText("Fleet health not read yet")).toBeNull();
    expect(screen.getAllByText("3").length).toBeGreaterThan(0);
  });
});

function renderCommunity(lockReason: AiLockReason | null, isAdmin = true) {
  viewer.isAdmin = isAdmin;
  return renderWithProviders(
    <MemoryRouter>
      <FleetActivityCard activity={activity([])} isLoading={false} licensed={false} lockReason={lockReason} />
    </MemoryRouter>,
  );
}

// B3 of dev-docs/ctem-open-source.md: in Community Edition the card says what
// fleets do with the one way forward, never "Open fleets" (a locked page).
describe("FleetActivityCard in Community Edition", () => {
  it("gives an administrator the Enterprise Edition state and the trial, no dead end", () => {
    renderCommunity("unlicensed");
    expect(screen.getByText("Fleets run with Enterprise Edition")).toBeTruthy();
    expect(screen.getByText("Start free trial")).toBeTruthy();
    expect(screen.queryByText("No fleet yet")).toBeNull();
    expect(screen.queryByRole("button", { name: /Open fleets/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^All$/ })).toBeNull();
    expect(screen.queryByText(/runs? in flight/)).toBeNull();
  });

  it("gives a non-administrator the sentence alone, no button", () => {
    renderCommunity("unlicensed", false);
    expect(screen.getByText("Ask your administrator to activate a license.")).toBeTruthy();
    expect(screen.queryByText("Start free trial")).toBeNull();
  });

  it("says AI off first when the platform's AI is off", () => {
    renderCommunity("ai_off");
    expect(screen.getByRole("link", { name: "Turn AI on" })).toBeTruthy();
    expect(screen.queryByText("Start free trial")).toBeNull();
  });

  it("keeps the licensed empty state with Open fleets", () => {
    viewer.isAdmin = true;
    renderCard(activity([]));
    expect(screen.getByText("No fleet yet")).toBeTruthy();
    expect(screen.queryByText("Fleets run with Enterprise Edition")).toBeNull();
  });
});

// #9566: fleets are opt-in; with every CTEM fleet off the card invites the
// setup instead of tiles of zeros, and never says Healthy.
describe("FleetActivityCard with fleets off", () => {
  const off = () => activity([tile("a", { optIn: "not_set_up", status: null }), tile("b", { optIn: "not_set_up", status: null })]);

  it("invites an administrator to set up each fleet, with no figure", () => {
    renderCard(off(), { canSetUp: true, onDismissInvite: () => {} });
    expect(screen.getByRole("heading", { name: "Add agentic work to this page" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Set up Fleet a" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Set up Fleet b" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Close the fleets invitation" })).toBeTruthy();
    expect(screen.queryByText("Healthy")).toBeNull();
    expect(screen.queryByText(/runs? in flight/)).toBeNull();
    expect(screen.queryByText("In flight")).toBeNull();
  });

  it("gives the documented range per month, and nothing for a fleet that documents none", () => {
    renderCard(off(), { canSetUp: true });
    expect(screen.getByText("About 8 to 35 runs per month")).toBeTruthy();
    expect(screen.getAllByText(/runs per month/)).toHaveLength(1);
    expect(screen.queryByText(/No run expected/)).toBeNull();
  });

  it("links every fleet from the header with its own wording", () => {
    renderCard(off(), { canSetUp: true });
    expect(screen.getByRole("button", { name: /View all fleets/ })).toBeTruthy();
  });

  it("tells a non-administrator who can set fleets up, with no button", () => {
    renderCard(off(), { canSetUp: false });
    expect(screen.getByText("Fleets are off: this page reads your connected modules only. An administrator can set them up.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Set up/ })).toBeNull();
  });

  it("once closed, keeps one quiet line", () => {
    renderCard(off(), { canSetUp: true, inviteShown: false });
    expect(screen.queryByRole("heading", { name: "Add agentic work to this page" })).toBeNull();
    expect(screen.getByText("Fleets are off: this page reads your connected modules only.")).toBeTruthy();
  });

  it("with one fleet on, shows the tiles with the opt-in state of each", () => {
    renderCard(activity([tile("a"), tile("b", { optIn: "not_set_up", status: null })]));
    // English tests render the message ids; en.json shows "On" (UX round 2 B2).
    expect(screen.getByText("On (fleet opt-in)")).toBeTruthy();
    expect(screen.getByText("Not set up")).toBeTruthy();
    expect(screen.getByText("Not shown while the fleet is not set up")).toBeTruthy();
    expect(screen.queryByText("Healthy")).toBeNull();
  });

  it("names the opt-in states with their own fleet keys, feminine in fr (UX round 2 B2)", () => {
    renderWithProviders(
      <MemoryRouter>
        <FleetActivityCard
          activity={activity([tile("a"), tile("b", { optIn: "paused", status: null }), tile("c", { optIn: "partial" })])}
          isLoading={false}
        />
      </MemoryRouter>,
      { locale: "fr" },
    );
    expect(screen.getByText("Active")).toBeTruthy();
    expect(screen.getByText("En pause")).toBeTruthy();
    expect(screen.getByText("Partiellement active")).toBeTruthy();
    expect(screen.queryByText("Activé")).toBeNull();
  });
});
