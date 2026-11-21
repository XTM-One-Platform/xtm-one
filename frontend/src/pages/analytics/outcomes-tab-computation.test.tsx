/**
 * The Analytics Outcomes tab says how the platform figures are reached, with
 * the same shared "How this is computed" block as the agent Overview and the
 * fleet ROI view (UX #9810 r4 B1; Standards s6, every ROI figure on every
 * screen opens how it is computed) - `yarn test:unit`.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import type { RoiReportResponse } from "@/hooks/api/useRoiReportApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";

import { OutcomesTab } from "./OutcomesTab";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), download: vi.fn() },
}));

const get = vi.mocked(api.get);

function report(): RoiReportResponse {
  return {
    window: "30d",
    currency: "USD",
    rate_label: "assumption",
    estimated: false,
    organization: {
      currency: "USD",
      runs: 5,
      units: 10,
      hours_saved: 2.83,
      llm_cost: 0.42,
      value: 251.4,
      net_value: 250.98,
      roi: null,
      reviewed: 3,
      unreviewed: 2,
      rejected: 1,
      review_rate: 0.6,
      assumptions: [],
      computation: [
        { kind: "saving", unit: "case", name: "Case triaged", units: 10, manual_min: 200, review_min: 30, minutes_saved: 170, rate: 88.73, value: 251.4, currency: "USD" },
        { kind: "total", minutes_saved: 170, hours_saved: 2.83, value: 251.4, llm_cost: 0.42, net_value: 250.98, currency: "USD" },
        { kind: "review", reviewed: 3, total: 5 },
        { kind: "rejected", count: 1 },
      ],
    },
    fleets: [],
    agents: [],
  };
}

afterEach(() => {
  get.mockReset();
  vi.restoreAllMocks();
});

describe("the Analytics Outcomes tab", () => {
  it("says how the platform figures are computed, as the agent and fleet views do (UX #9810 r4 B1)", async () => {
    get.mockImplementation(async () => report());
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderWithProviders(
      <MemoryRouter initialEntries={["/observability/outcomes"]}>
        <OutcomesTab />
      </MemoryRouter>,
      client,
    );
    expect(await screen.findByText("How this is computed")).toBeTruthy();
    expect(get).toHaveBeenCalledWith("/roi?window=30d");
    const lines = screen.getByTestId("roi-computation").textContent ?? "";
    expect(lines).toContain("Case triaged: 10 units, 200 manual min minus 30 review min = 170 min saved");
    expect(lines).toContain("170 min = 2.8 h");
    expect(lines).toContain("Review rate: 3 reviewed of 5 records");
    expect(lines).toContain("1 rejected run counts no time saved, only its cost.");
  });
});
