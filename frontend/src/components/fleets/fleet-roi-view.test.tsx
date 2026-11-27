/**
 * The fleet page's ROI view and its shared window (#9785, slice O8b of #9206;
 * design dev-docs/roi-outcomes.md 6.3) - `yarn test:unit` (vitest + happy-dom).
 * The view and the cost card read one `?window=`, so they never show two
 * different periods (R2-A2).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import type { RoiReportResponse } from "@/hooks/api/useRoiReportApi";
import { api } from "@/lib/api";
import {
  DEFAULT_FLEET_WINDOW,
  fleetWindowDays,
  readFleetWindow,
  widerFleetWindow,
  withFleetWindow,
} from "@/lib/fleet-roi-window";
import { fleetRoiState, ledgerAgentRows, outcomeRowPath, scopeHeroFigures } from "@/lib/roi-outcomes";
import { renderWithProviders } from "@/testing/providers";

import { FleetCostCard } from "./FleetCostCard";
import { FleetRoiView } from "./FleetRoiView";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), download: vi.fn() },
}));

const get = vi.mocked(api.get);

const figures = (runs: number, extra: Partial<RoiReportResponse["organization"]> = {}) => ({
  currency: "EUR",
  runs,
  units: runs,
  hours_saved: runs * 2,
  llm_cost: 0.5,
  value: runs * 100,
  net_value: runs * 100,
  roi: null,
  reviewed: 0,
  unreviewed: runs,
  rejected: 0,
  review_rate: runs > 0 ? 0 : null,
  assumptions: [],
  ...extra,
});

function report(runs: number): RoiReportResponse {
  return {
    window: "30d",
    currency: "EUR",
    rate_label: "assumption",
    estimated: false,
    organization: figures(runs),
    fleets: runs > 0 ? [{ id: "f-1", name: "Contracts", ...figures(runs) }] : [],
    agents: runs > 0 ? [{ id: "a-1", name: "Contract reviewer", ...figures(runs) }] : [],
  };
}

const cost = { fleet_id: "f-1", days: 30, since: "2026-10-08T00:00:00Z", total_cost: 1.25, total_tokens: 10, trace_count: 3, agents: [] };

/** Routes the mocked GETs: the cost card, the window's report, the lifetime report. */
function serve({ windowRuns, lifetimeRuns = windowRuns, fail = false }: { windowRuns: number; lifetimeRuns?: number; fail?: boolean }) {
  get.mockImplementation(async (url: string) => {
    if (url.startsWith("/fleets/")) return cost;
    // The chat series is its own read (#10544 S5): an empty one here.
    if (url.startsWith("/roi/chat")) return { currency: "EUR", totals: { deliverables: 0, pending: 0, corrected: 0, minutes: 0, hours: 0, value: 0 }, lines: [] };
    if (fail) throw Object.assign(new Error("boom"), { status: 500 });
    return url.includes("window=all") ? report(lifetimeRuns) : report(windowRuns);
  });
}

function renderPage(url: string, onSetFleetDefault?: () => void) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: 3, throwOnError: true } } });
  return renderWithProviders(
    <MemoryRouter initialEntries={[url]}>
      <FleetRoiView fleetId="f-1" onSetFleetDefault={onSetFleetDefault} />
      <FleetCostCard fleetId="f-1" />
    </MemoryRouter>,
    client,
  );
}

afterEach(() => {
  get.mockReset();
  vi.restoreAllMocks();
});

describe("the shared fleet window", () => {
  it("reads ?window=, 30 days when absent or unknown", () => {
    expect(readFleetWindow(new URLSearchParams(""))).toBe(DEFAULT_FLEET_WINDOW);
    expect(readFleetWindow(new URLSearchParams("window=7d"))).toBe("7d");
    expect(readFleetWindow(new URLSearchParams("window=90d"))).toBe("90d");
    expect(readFleetWindow(new URLSearchParams("window=all"))).toBe("30d");
    expect(readFleetWindow(new URLSearchParams("window=x"))).toBe("30d");
  });

  it("writes the default as no parameter and keeps the rest of the query", () => {
    expect(withFleetWindow(new URLSearchParams("view=roi&window=7d"), "30d").toString()).toBe("view=roi");
    expect(withFleetWindow(new URLSearchParams("view=roi"), "90d").toString()).toBe("view=roi&window=90d");
  });

  it("maps a window to the cost API's days and widens up to 90 days", () => {
    expect(fleetWindowDays("7d")).toBe(7);
    expect(fleetWindowDays("90d")).toBe(90);
    expect(widerFleetWindow("7d")).toBe("30d");
    expect(widerFleetWindow("90d")).toBeNull();
  });
});

describe("fleetRoiState", () => {
  it("tells never measured from nothing in this period through the lifetime report", () => {
    expect(fleetRoiState(report(2), undefined)).toBe("data");
    expect(fleetRoiState(report(0), undefined)).toBeNull();
    expect(fleetRoiState(report(0), report(0))).toBe("none");
    expect(fleetRoiState(report(0), report(4))).toBe("window");
  });

  it("reads emptiness from the ledger: a chat-only fleet draws its hero (#12077 L6b3 UX r1 B2)", () => {
    const t = { runs: 0, reviewed: 0, units: 0, deliverables: 3, hours: 5, hours_low: 2, hours_high: 8, value: 450, value_low: 200, value_high: 700 };
    const costs = { ai_cost: 1, net_value: 449, net_value_low: 199, net_value_high: 699, roi: null, payback_months: null };
    const ledger = { scope: "fleet", currency: "EUR", totals: { ...t, ...costs }, by_source: [], by_unit: [], by_agent: [], by_fleet: [], by_week: [], by_person: null, cost_masked: false, since: null };
    const chatOnly: RoiReportResponse = { ...report(0), figures: ledger };
    expect(fleetRoiState(chatOnly, undefined)).toBe("data");
    expect(fleetRoiState(report(0), chatOnly)).toBe("window");
  });
});

describe("the fleet ROI view", () => {
  it("reads the fleet's report on the page's window, as the cost card does", async () => {
    serve({ windowRuns: 2 });
    renderPage("/fleets/f-1/roi?window=7d");
    expect(await screen.findByText("Contract reviewer")).toBeTruthy();
    expect(get).toHaveBeenCalledWith("/roi?scope=fleet&id=f-1&window=7d");
    expect(get).toHaveBeenCalledWith("/fleets/f-1/cost?days=7");
    // One row per agent: the fleet's own figures are the KPI cards.
    expect(screen.queryByText("Contracts")).toBeNull();
  });

  it("opens where the agent lives from the row, its name plain text (UX #9790 B2, #12070)", async () => {
    serve({ windowRuns: 2 });
    renderPage("/fleets/f-1/roi");
    const name = await screen.findByTitle("Contract reviewer");
    // The row's own link (rowHref) wraps the name; the name itself is no link of its own.
    const rowLink = name.closest("a");
    expect(rowLink?.getAttribute("href")).toBe("/agents/a-1/value");
    expect(rowLink?.parentElement?.closest("a") ?? null).toBeNull();
    expect(name.tagName).not.toBe("A");
    expect(outcomeRowPath({ kind: "fleet", id: "f-1" })).toBe("/fleets/f-1");
  });

  it("moves the cost card with it when the window changes", async () => {
    serve({ windowRuns: 2 });
    renderPage("/fleets/f-1/roi");
    await screen.findByText("Contract reviewer");
    const [roiWindow] = screen.getAllByRole("radio", { name: "Last 90 days" });
    fireEvent.click(roiWindow);
    await vi.waitFor(() => expect(get).toHaveBeenCalledWith("/fleets/f-1/cost?days=90"));
    expect(get).toHaveBeenCalledWith("/roi?scope=fleet&id=f-1&window=90d");
    for (const radio of screen.getAllByRole("radio", { name: "Last 90 days" })) {
      expect(radio.getAttribute("aria-checked")).toBe("true");
    }
  });

  it("says a failed read in its own line and keeps the page", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    serve({ windowRuns: 0, fail: true });
    renderPage("/fleets/f-1/roi");
    expect(await screen.findByText("The ROI of this fleet could not be read.")).toBeTruthy();
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.getByText("Fleet cost")).toBeTruthy();
  });

  it("offers the fleet default when the fleet never measured anything", async () => {
    serve({ windowRuns: 0, lifetimeRuns: 0 });
    const onSetFleetDefault = vi.fn();
    renderPage("/fleets/f-1/roi", onSetFleetDefault);
    expect(await screen.findByText("This fleet measures nothing yet.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Set the fleet default" }));
    expect(onSetFleetDefault).toHaveBeenCalledTimes(1);
  });

  it("offers a wider window when only this period is empty", async () => {
    serve({ windowRuns: 0, lifetimeRuns: 3 });
    renderPage("/fleets/f-1/roi?window=7d");
    expect(await screen.findByText("Nothing was counted in this period.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Widen the window" }));
    await vi.waitFor(() => expect(get).toHaveBeenCalledWith("/roi?scope=fleet&id=f-1&window=30d"));
  });

  it("says the value is measured since the reset when the fleet was reset in the window (#2580)", async () => {
    get.mockImplementation(async (url: string) => {
      if (url.startsWith("/fleets/")) return cost;
      if (url.startsWith("/roi/chat")) return { currency: "EUR", totals: { deliverables: 0, pending: 0, corrected: 0, minutes: 0, hours: 0, value: 0 }, lines: [] };
      return { ...report(0), value_measured_since: "2026-11-27T08:00:00Z" };
    });
    renderPage("/fleets/f-1/roi");
    expect(await screen.findByTestId("roi-reset-note")).toHaveTextContent(/Measured since .*2026.* \(reset\)/);
  });

  it("draws no reset line for a fleet never reset", async () => {
    serve({ windowRuns: 2 });
    renderPage("/fleets/f-1/roi");
    await screen.findByText("Contract reviewer");
    expect(screen.queryByTestId("roi-reset-note")).toBeNull();
  });

  it("exports the fleet's report, not the platform's", async () => {
    serve({ windowRuns: 2 });
    const download = vi.mocked(api.download);
    download.mockResolvedValue({ blob: new Blob(["x"]), filename: "agents-value-f-1.csv" });
    renderPage("/fleets/f-1/roi");
    await screen.findByText("Contract reviewer");
    fireEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    await vi.waitFor(() => expect(download).toHaveBeenCalledWith("/roi/export?format=csv&window=30d&fleet_id=f-1"));
  });
});

describe("the fleet ROI view on the ledger (#12077 L6b3 UX r2)", () => {
  const t = (hours: number, value: number, runs = 0, deliverables = 0) => ({
    runs,
    reviewed: 0,
    units: runs,
    deliverables,
    hours,
    hours_low: hours,
    hours_high: hours,
    value,
    value_low: value,
    value_high: value,
    ai_cost: 1,
    net_value: value - 1,
    net_value_low: value - 1,
    net_value_high: value - 1,
    roi: null,
    payback_months: null,
  });
  const ledger = (suppressed: NonNullable<RoiReportResponse["figures"]>["suppressed"] = null) => ({
    scope: "fleet",
    currency: "EUR",
    totals: { ...t(1089, 61000, 3, 2), ai_cost: 2, net_value: 60998 },
    by_source: [{ key: "automation", ...t(3, 300, 3) }, { key: "chat", ...t(1086, 60700, 0, 2) }],
    by_unit: [],
    by_agent: [{ key: "a-1", ...t(3, 300, 3) }, { key: "a-2", ...t(1086, 60700, 0, 2) }],
    by_fleet: [],
    by_week: [],
    by_person: null,
    cost_masked: false,
    since: null,
    names: { "a-1": "Orchestrator", "a-2": "Researcher" },
    suppressed,
  });

  it("draws one row per agent of the ledger, the chat-only agent included, and names the chat deliverables in the caption (B4, B6)", () => {
    const figures = ledger();
    const rows = ledgerAgentRows({ ...report(3), figures });
    expect(rows?.rows.map((r) => [r.name, r.hoursSaved])).toEqual([
      ["Researcher", 1086],
      ["Orchestrator", 3],
    ]);
    const sum = rows!.rows.reduce((s, r) => s + r.hoursSaved, 0) + rows!.remainders.reduce((s, r) => s + r.row.hoursSaved, 0);
    expect(sum).toBe(figures.totals.hours);
  });

  it("keeps the source and total steps under k, so the own-share popover sums to the hero (B5)", () => {
    const data = { ...report(3), figures: ledger({ k: 5, groups: 0, own_share_only: true, sources_split: true, weeks_merged: 0 }) };
    const hero = scopeHeroFigures(data);
    expect(hero.computation?.map((s) => s.kind)).toEqual(["source", "source", "total"]);
    expect(hero.assumption_facts).toEqual([]);
  });

  it("reads the caption's runs and chat deliverables from the ledger hero (B4)", async () => {
    get.mockImplementation(async (url: string) => {
      if (url.startsWith("/fleets/")) return cost;
      if (url.startsWith("/roi/chat")) return { currency: "EUR", totals: { deliverables: 0, pending: 0, corrected: 0, minutes: 0, hours: 0, value: 0 }, lines: [] };
      return { ...report(3), figures: ledger() };
    });
    renderPage("/fleets/f-1/roi");
    expect(await screen.findByText("Researcher")).toBeTruthy();
    expect(screen.getAllByText("3 runs, 2 chat deliverables").length).toBeGreaterThan(0);
  });

  it("states the fleet's scope and shows an outside supervisor's cost apart, never in the cards (#2639)", async () => {
    get.mockImplementation(async (url: string) => {
      if (url.startsWith("/fleets/")) return cost;
      if (url.startsWith("/roi/chat")) return { currency: "EUR", totals: { deliverables: 0, pending: 0, corrected: 0, minutes: 0, hours: 0, value: 0 }, lines: [] };
      return { ...report(3), figures: { ...ledger(), basis: "fleet_members", supervision: { agents: 1, ...t(0, 0), ai_cost: 12 } } };
    });
    renderPage("/fleets/f-1/roi");
    expect(await screen.findByText("This fleet's runs only. The organization total counts every agent.")).toBeTruthy();
    const line = screen.getByTestId("roi-fleet-supervision");
    expect(line.textContent).toContain("1 outside agent supervises this fleet");
    expect(line.textContent).toContain("12");
  });

  it("draws no supervision line when no outside agent supervises the fleet (#2639)", async () => {
    get.mockImplementation(async (url: string) => {
      if (url.startsWith("/fleets/")) return cost;
      if (url.startsWith("/roi/chat")) return { currency: "EUR", totals: { deliverables: 0, pending: 0, corrected: 0, minutes: 0, hours: 0, value: 0 }, lines: [] };
      return { ...report(3), figures: { ...ledger(), basis: "fleet_members", supervision: null } };
    });
    renderPage("/fleets/f-1/roi");
    expect(await screen.findByTestId("roi-fleet-scope-note")).toBeTruthy();
    expect(screen.queryByTestId("roi-fleet-supervision")).toBeNull();
  });
});
