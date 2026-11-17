/**
 * The list column cell (#9836 R6b, design 10.8; ledger since #12077 L7a): the shared figure
 * without its range, drawn from the ledger's figures of the object (the agent page hero's),
 * "-" with the reason when nothing is declared, "Declare a unit" only for a reader who may
 * edit the agent, "Hidden" never 0 when costs are masked; under k (L7f, design 12.10) "-"
 * with the reader's own use or the hidden-totals reason, never a figure.
 */
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { RoiListCell } from "@/components/roi/RoiListCell";
import type { RoiLedgerTotals, RoiSummaryItem } from "@/hooks/api/useRoiReportApi";
import { renderWithProviders } from "@/testing/providers";

const totals: RoiLedgerTotals = {
  runs: 12,
  reviewed: 4,
  units: 16,
  deliverables: 0,
  hours: 9.5,
  hours_low: 6,
  hours_high: 14,
  value: 712.5,
  value_low: 450,
  value_high: 1050,
  ai_cost: 1.5,
  net_value: 711,
  net_value_low: 448.5,
  net_value_high: 1048.5,
  roi: 474,
  payback_months: 0,
};

const item: RoiSummaryItem = {
  id: "a1",
  declared: true,
  figures: {
    scope: "agent",
    currency: "USD",
    totals,
    by_source: [{ key: "automation", ...totals }],
    cost_masked: false,
    suppressed: null,
  },
};

describe("RoiListCell", () => {
  it("draws the net value without its range, the computation one click away", () => {
    renderWithProviders(<RoiListCell item={item} currency="USD" costMasked={false} />);
    const cell = screen.getByTestId("roi-figure-net");
    expect(cell.textContent).toContain("$711");
    expect(cell.textContent).not.toContain("to");
    expect(screen.getByRole("button", { name: /How this is computed/ })).toBeTruthy();
  });

  it("writes the ledger's source lines and total in its popover, as the agent page hero does", () => {
    renderWithProviders(<RoiListCell item={item} currency="USD" costMasked={false} />);
    fireEvent.click(screen.getByTestId("roi-how-net"));
    const popover = screen.getByTestId("roi-how-popover");
    expect(popover.textContent).toContain("$711");
  });

  it("draws the gross value when costs are masked, never 0", () => {
    const masked: RoiSummaryItem = {
      ...item,
      figures: { ...item.figures!, cost_masked: true, totals: { ...totals, ai_cost: null, net_value: null, net_value_low: null, net_value_high: null, roi: null, payback_months: null } },
    };
    renderWithProviders(<RoiListCell item={masked} currency="USD" costMasked />);
    expect(screen.queryByTestId("roi-figure-net")).toBeNull();
    expect(screen.getByTestId("roi-figure-value").textContent).toContain("$713");
  });

  const underK = (used: boolean, masked = false): RoiSummaryItem => {
    const own: RoiLedgerTotals = used
      ? { ...totals, value: 20800, net_value: masked ? null : 20790, ai_cost: masked ? null : 10 }
      : { ...totals, runs: 0, reviewed: 0, units: 0, hours: 0, hours_low: 0, hours_high: 0, value: 0, value_low: 0, value_high: 0, ai_cost: 0, net_value: 0, net_value_low: 0, net_value_high: 0, roi: null, payback_months: null };
    return { ...item, figures: { ...item.figures!, totals: own, cost_masked: masked, suppressed: { k: 5, groups: 0, own_share_only: true, sources_split: true, weeks_merged: 0 } } };
  };

  it("draws no figure under k but the reader's own use in its tip", async () => {
    renderWithProviders(<RoiListCell item={underK(true)} currency="USD" costMasked={false} />);
    const cell = screen.getByTestId("roi-list-cell-under-k");
    expect(cell.textContent?.trim()).toBe("-");
    expect(screen.queryByTestId("roi-figure-net")).toBeNull();
    fireEvent.focus(screen.getByRole("img", { name: "Why the totals are hidden" }));
    expect((await screen.findAllByText("Your use: $20.8K; totals shown from 5 people")).length).toBeGreaterThan(0);
  });

  it("says the totals are hidden under k when the reader did not use it, costs masked too", async () => {
    renderWithProviders(<RoiListCell item={underK(false, true)} currency="USD" costMasked />);
    expect(screen.getByTestId("roi-list-cell-under-k").textContent?.trim()).toBe("-");
    expect(screen.queryByTestId("roi-figure-value")).toBeNull();
    fireEvent.focus(screen.getByRole("img", { name: "Why the totals are hidden" }));
    expect((await screen.findAllByText("Fewer than 5 people: hidden")).length).toBeGreaterThan(0);
  });

  it("offers Declare a unit only to a reader who may edit the agent", () => {
    const { unmount } = renderWithProviders(
      <MemoryRouter>
        <RoiListCell item={{ id: "a4", declared: false }} currency="USD" costMasked={false} declareAgentId="a4" />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("roi-list-cell-empty").textContent).toContain("-");
    expect(screen.getByRole("link", { name: "Declare a unit" }).getAttribute("href")).toContain("/agents/a4/config/outcomes");
    unmount();
    renderWithProviders(<RoiListCell item={{ id: "a4", declared: false }} currency="USD" costMasked={false} />);
    expect(screen.queryByRole("link", { name: "Declare a unit" })).toBeNull();
  });
});
