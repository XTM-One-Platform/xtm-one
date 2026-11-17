/**
 * The ROI cards' cost line (#9837 A1, advisory of the UX round 1 on #9810) -
 * `yarn test:unit`. The cost under the net value is written in the reader's
 * locale and the report currency, as the net value next to it, never as a
 * bare "$0.42".
 */
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import type { RoiReportResponse } from "@/hooks/api/useRoiReportApi";
import { renderWithProviders } from "@/testing/providers";

import { RoiKpiCards } from "./RoiReportParts";

const figures = (llmCost: number): RoiReportResponse["organization"] => ({
  currency: "USD",
  runs: 4,
  units: 4,
  hours_saved: 2.8,
  llm_cost: llmCost,
  value: 251.4,
  net_value: 251,
  roi: null,
  reviewed: 1,
  unreviewed: 3,
  rejected: 0,
  review_rate: 0.25,
  assumptions: [],
});

/** The text a locale writes for an amount, with its spaces made plain. */
const plain = (text: string) => text.replace(/[\u00a0\u202f]/g, " ");

describe("the ROI cards' cost line", () => {
  it("writes the cost in the reader's locale and the report currency (fr)", () => {
    renderWithProviders(<RoiKpiCards figures={figures(0.42)} currency="USD" window="30d" />, { locale: "fr" });
    const card = plain(screen.getByText(/0,42/).textContent ?? "");
    expect(card).toContain("0,42 $US");
    expect(card).not.toContain("$0.42");
  });

  it("keeps four decimals below one cent", () => {
    renderWithProviders(<RoiKpiCards figures={figures(0.0042)} currency="EUR" window="30d" />);
    expect(screen.getByText(/0\.0042/).textContent).toContain("€0.0042");
  });
});
