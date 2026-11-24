/**
 * The home's Top value drivers card (#12009) - `yarn test:unit`. The agents of
 * the Value card's report, most hours first, each opening its agent; a deleted
 * agent named and unlinked; the empty, Enterprise and error states.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { RoiGroupFigures, RoiReportResponse } from "@/hooks/api/useRoiReportApi";
import { renderWithProviders } from "@/testing/providers";

const state = vi.hoisted(() => ({
  report: { data: undefined, isError: false, refetch: () => undefined } as { data?: Partial<RoiReportResponse>; isError: boolean; refetch: () => void },
  locked: null as string | null,
}));

vi.mock("@/hooks/api/useRoiReportApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useRoiReportApi")>()),
  useValueReport: () => state.report,
}));
vi.mock("@/hooks/api/useMiscApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useMiscApi")>()),
  useDashboardStats: () => ({ data: { agent_summaries: [{ id: "a-1", icon: "Mail" }] } }),
}));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: { id: "u-1" }, isAdmin: true }) }));
vi.mock("@/hooks/useEnterpriseUpsell", () => ({ useEnterpriseLockedReason: () => state.locked }));

import { valueDrivers } from "@/lib/roi-value";
import { RoiValueDriversCard } from "./RoiValueDriversCard";

const agent = (id: string, name: string, hours: number, runs = 3): RoiGroupFigures =>
  ({ id, name, hours_saved: hours, runs }) as RoiGroupFigures;

const render = () =>
  renderWithProviders(
    <MemoryRouter>
      <RoiValueDriversCard />
    </MemoryRouter>,
  );

beforeEach(() => {
  state.report = { data: undefined, isError: false, refetch: () => undefined };
  state.locked = null;
});

describe("valueDrivers", () => {
  it("keeps the agents that saved time, most hours first, ties by name", () => {
    const rows = valueDrivers([agent("a", "Zed", 2), agent("b", "Alpha", 2), agent("c", "None", 0), agent("d", "Top", 9)]);
    expect(rows.map((row) => row.name)).toEqual(["Top", "Alpha", "Zed"]);
  });
});

describe("the Top value drivers card", () => {
  it("lists the agents, each opening its agent, and links All agents to the By agent tab", () => {
    state.report = { isError: false, refetch: () => undefined, data: { currency: "EUR", agents: [agent("a-1", "Email triage", 12, 40), agent("a-2", "Digest", 30, 5)] } };
    render();
    const links = screen.getAllByTestId("home-value-driver");
    expect(links.map((link) => link.getAttribute("href"))).toEqual(["/agents/a-2", "/agents/a-1"]);
    expect(screen.getByTestId("home-value-drivers-all").getAttribute("href")).toBe("/tasks/value/agents");
  });

  it("draws a deleted agent named and unlinked", () => {
    state.report = { isError: false, refetch: () => undefined, data: { currency: "EUR", agents: [agent("", "deleted agent", 4)] } };
    render();
    expect(screen.queryByTestId("home-value-driver")).toBeNull();
    expect(screen.getByTestId("home-value-driver-deleted").textContent).toContain("Deleted agent");
  });

  it("says no value is recorded yet and offers the methodology", () => {
    state.report = { isError: false, refetch: () => undefined, data: { currency: "EUR", agents: [] } };
    render();
    expect(screen.getByText("No value recorded yet")).toBeTruthy();
    expect(screen.getByText("Declare a unit of work").closest("a")?.getAttribute("href")).toBe("/tasks/value/methodology");
  });

  it("draws the empty state compact, so it fits the body of the widget at its minimum height (h6)", () => {
    state.report = { isError: false, refetch: () => undefined, data: { currency: "EUR", agents: [] } };
    const { container } = render();
    expect(container.querySelector('[data-slot="empty-state"]')?.getAttribute("data-density")).toBe("compact");
  });

  it("says where Value lives on Community Edition", () => {
    state.locked = "enterprise";
    render();
    expect(screen.getByText("Value is part of Enterprise Edition.")).toBeTruthy();
  });

  it("offers a retry when the report cannot be read", () => {
    state.report = { isError: true, refetch: () => undefined };
    render();
    expect(screen.getByRole("alert").textContent).toContain("The value could not be read.");
  });
});
