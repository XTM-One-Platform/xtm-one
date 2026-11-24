// The hub card body shared by /agents-hub, /agent-studio, /work and
// /tools-and-integrations (#9301 N2c, UX round 1 B1-B3 on #10393).

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { ResourceHub, type HubResource } from "@/components/ui/resource-hub";
import { renderWithProviders } from "@/testing/providers";

const agent = { id: "a1", name: "ROI analyst", updated_at: "2026-11-09T10:00:00Z" };

function renderHub(resources: HubResource[]) {
  renderWithProviders(
    <MemoryRouter>
      <ResourceHub title="Agents" description="Your agents." resources={resources} />
    </MemoryRouter>,
  );
}

const agentsCard: HubResource = {
  to: "/agents",
  icon: "bot",
  label: "Agents",
  description: "The assistants.",
  unit: ["agent", "agents"],
  items: [agent],
  detailTo: (item) => `/agents/${item.id}`,
};

describe("ResourceHub", () => {
  it("lists an object once in Recently modified when another card reuses its items", () => {
    renderHub([
      agentsCard,
      { to: "/flows", icon: "route", label: "Agentic Flow", description: "The map.", unit: ["agent", "agents"], items: [agent], inRecent: false },
    ]);
    expect(screen.getAllByText("ROI analyst")).toHaveLength(1);
  });

  it("shows no count and no date on a locked card, never a false 0", () => {
    renderHub([
      agentsCard,
      { to: "/fleets", icon: "boxes", label: "Fleets", description: "Teams of agents.", unit: ["fleet", "fleets"], items: undefined, locked: true, lockReason: "unlicensed" },
    ]);
    expect(screen.getByText("Unlicensed")).toBeTruthy();
    expect(screen.queryByText(/^\d+ fleets?$/)).toBeNull();
    expect(screen.getByText("1 agent")).toBeTruthy();
  });

  it("offers a create menu behind a Create button named like the other cards", () => {
    renderHub([
      { ...agentsCard, to: "/fleets", label: "Fleets", createMenu: [{ label: "New fleet", onSelect: () => {} }] },
    ]);
    expect(screen.getByRole("button", { name: "Create" })).toBeTruthy();
  });
});
