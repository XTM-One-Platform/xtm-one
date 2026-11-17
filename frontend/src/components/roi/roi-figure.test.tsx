/**
 * The one ROI figure (#9836 R4b, design dev-docs/roi-outcomes.md 10.5 and
 * 10.6) - `yarn test:unit`. The range caption, "Hidden" for a masked cost
 * (never 0), the vocabulary of the input labels, and the popover whose
 * arithmetic, recomputed here from the inputs it shows, equals the figure.
 */
import { describe, expect, it } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { API_LABEL_SHOWN, roiComputation, roiRecordCounts, shownLabel, type RoiFigures } from "@/lib/roi-figure";

import { RoiFigure } from "./RoiFigure";
import { RoiAssumptions, RoiKpiCards } from "./RoiReportParts";

const figures = (over: Partial<RoiFigures> = {}): RoiFigures => ({
  currency: "USD",
  runs: 17,
  units: 42,
  hours_saved: 21,
  llm_cost: 38,
  value: 1995,
  net_value: 1957,
  roi: 51.5,
  payback_months: 0,
  range: { hours_saved: [14, 31.5], value: [1330, 2992.5], net_value: [1292, 2954.5], roi: [34, 77.75], payback_months: [0, 0] },
  reviewed: 8,
  unreviewed: 9,
  rejected: 3,
  review_rate: 0.47,
  assumptions: [],
  computation: [
    { kind: "saving", unit: "triage", name: "Alert triage", units: 42, manual_min: 1470, review_min: 210, minutes_saved: 1260, rate: 95, value: 1995, currency: "USD" },
  ],
  assumption_facts: [
    { kind: "unit", unit: "triage", name: "Alert triage", manual_min: 35, manual_min_low: 25, manual_min_high: 50, review_min: 5, label: "published estimate", source: "Industry SOC survey" },
    { kind: "rate", role: "analyst", amount: 95, currency: "USD", label: "organization override", source: "organization rates by role" },
  ],
  ...over,
});

/** The text a locale writes, with its spaces made plain. */
const plain = (text: string | null) => (text ?? "").replace(/[\u00a0\u202f]/g, " ");

describe("RoiFigure", () => {
  it("draws the likely value and the range as the card caption", () => {
    renderWithProviders(<RoiFigure figures={figures()} metric="hours" currency="USD" window="30d" layout="card" />);
    const card = screen.getByTestId("roi-figure-hours");
    expect(plain(within(card).getByTestId("stat-card-value").textContent)).toBe("21 h");
    expect(plain(within(card).getByTestId("roi-figure-hours-caption").textContent)).toBe("14 to 31.5 h");
  });

  it("writes the range compact in French with the locale's spacing, the exact range on hover (#10476)", () => {
    renderWithProviders(<RoiFigure figures={figures()} metric="value" currency="EUR" window="30d" layout="inline" />, { locale: "fr" });
    expect(plain(screen.getByTestId("roi-figure-value").textContent)).toContain("1,3 k € à 3 k €");
    expect(plain(screen.getByTitle(/^1\s330,00\s€ à 2\s992,50\s€$/).textContent)).toBe("1,3 k € à 3 k €");
  });

  it("reads Hidden for a masked cost, never 0, with the reason beside it", () => {
    const masked = figures({ llm_cost: null, net_value: null, roi: null, payback_months: null });
    renderWithProviders(<RoiFigure figures={masked} metric="net" currency="USD" window="30d" costMasked layout="card" />);
    const card = screen.getByTestId("roi-figure-net");
    expect(within(card).getByTestId("stat-card-value").textContent).toBe("Hidden");
    expect(card.textContent).not.toMatch(/\$0/);
    expect(screen.getByRole("img", { name: "Why this figure is hidden" })).toBeTruthy();
  });

  it("shows the basis, not the value twice, when a payback range collapses", () => {
    renderWithProviders(<RoiFigure figures={figures()} metric="payback" currency="USD" window="30d" layout="card" />);
    const card = screen.getByTestId("roi-figure-payback");
    expect(within(card).getByTestId("stat-card-value").textContent).toBe("Under a month");
    expect(within(card).getByTestId("roi-figure-payback-caption").textContent).toBe("No setup cost");
  });
});

describe("How this is computed", () => {
  it("opens from the info button and reproduces the figure from the inputs it shows", () => {
    renderWithProviders(<RoiFigure figures={figures()} metric="net" currency="USD" window="30d" layout="card" />);
    fireEvent.click(screen.getByTestId("roi-how-net"));
    const popover = screen.getByTestId("roi-how-popover");
    expect(within(popover).getByText("Last 30 days")).toBeTruthy();
    const numbers = within(popover).getAllByTestId("roi-how-numbers").map((n) => plain(n.textContent));
    // The API sends TOTAL minutes (1470 manual, 210 review for 42 units): the line writes one unit's (UX r1 B1).
    expect(numbers[0]).toBe("42 x (35 - 5) min per unit x $95.00/h = $1,995.00");
    // Recompute the figure from the numbers the line shows, parsed back out of it.
    const [, units, manual, review, rate, value] = /^(\d+) x \(([\d.]+) - ([\d.]+)\) min per unit x \$([\d.]+)\/h = \$([\d,.]+)$/.exec(numbers[0])!;
    expect((Number(units) * (Number(manual) - Number(review)) * Number(rate)) / 60).toBeCloseTo(Number(value.replace(/,/g, "")), 2);
    expect(numbers[1]).toBe("$1,995.00 - $38.00 = $1,957.00");
    expect(numbers[2]).toBe("$1,957.00 / $38.00 = 51.5 x");
    expect(within(popover).getByText("Published estimate")).toBeTruthy();
    expect(within(popover).getByText("Customized")).toBeTruthy();
    // A shipped rate role reads by its name, never its key (UX r1 A2); the source is written out, not hover-only (B3).
    expect(popover.textContent).toContain("Hourly rate (Analyst)");
    expect(within(popover).getByText("Industry SOC survey")).toBeTruthy();
    expect(popover.textContent).not.toMatch(/override/i);
    // The AI cost is every model call of the period, chat included (dev-docs/roi-methodology.md section 4; UX #12247 r1 A1).
    expect(within(popover).getByText("Each model call of this period, chat and other usage included")).toBeTruthy();
    expect(popover.textContent).not.toContain("Model usage of these runs");
  });

  it("never opens the card it sits in", () => {
    let opened = 0;
    renderWithProviders(
      <RoiFigure figures={figures()} metric="review_rate" currency="USD" window="7d" layout="card" onClick={() => (opened += 1)} />,
    );
    fireEvent.click(screen.getByTestId("roi-how-review_rate"));
    expect(opened).toBe(0);
  });
});

describe("the label vocabulary", () => {
  it("maps every API label to a shown label, never Override", () => {
    for (const label of ["assumption", "published estimate", "measured", "organization override", "organization unit", "organization rates by role", "catalog default"]) {
      expect(label in API_LABEL_SHOWN).toBe(true);
    }
    expect(shownLabel("organization override; organization rates by role")).toBe("customized");
    expect(shownLabel("catalog default; organization rates by role")).toBe("default");
  });

  it("computes the popover sums from the report's own numbers", () => {
    const c = roiComputation(figures(), false);
    expect(c.value).toBe(1995);
    expect(c.net).toBe(1957);
    expect(c.roi).toBeCloseTo(51.5, 1);
    expect(roiComputation(figures(), true).net).toBeNull();
  });
});

describe("one figure on every surface", () => {
  it("draws the same strings in the KPI cards as the figure alone", () => {
    const { unmount } = renderWithProviders(<RoiFigure figures={figures()} metric="net" currency="USD" window="30d" layout="card" />);
    const alone = plain(screen.getByTestId("roi-figure-net").querySelector("[data-testid=stat-card-value]")?.textContent ?? "");
    unmount();
    renderWithProviders(<RoiKpiCards figures={figures()} currency="USD" window="30d" />);
    expect(plain(within(screen.getByTestId("roi-figure-net")).getByTestId("stat-card-value").textContent)).toBe(alone);
  });

  it("counts a work item apart from the runs in the card and in the popover alike (UX #9961 r2 B1)", () => {
    // 12 records: 11 runs and 1 work item. The card and "How this is computed" write the same split.
    const withItem = figures({ runs: 12, work_items: 1, range: undefined });
    renderWithProviders(<RoiKpiCards figures={withItem} currency="USD" window="30d" />);
    expect(screen.getByTestId("roi-figure-hours").textContent).toContain("11 runs, 1 work item");
    fireEvent.click(screen.getByTestId("roi-how-hours"));
    const popover = screen.getByTestId("roi-how-popover");
    expect(popover.textContent).toContain("Counted: 42 units from 11 runs and 1 work item");
    expect(popover.textContent).not.toContain("12 runs");
  });

  it("keeps runs, work items and chat deliverables apart in the card and the popover alike (UX #12157 r3 B8)", () => {
    // 13 records (12 runs, 1 work item) and 2 chat deliverables: the card and the popover write the same split.
    const withChats = figures({ runs: 13, work_items: 1, deliverables: 2, range: undefined });
    renderWithProviders(<RoiKpiCards figures={withChats} currency="USD" window="30d" />);
    expect(plain(screen.getByTestId("roi-figure-hours-caption").textContent)).toBe("12 runs, 1 work item, 2 chat deliverables");
    fireEvent.click(screen.getByTestId("roi-how-hours"));
    const popover = screen.getByTestId("roi-how-popover");
    expect(popover.textContent).toContain("from 12 runs, 1 work item and 2 chat deliverables");
    expect(popover.textContent).not.toContain("13 runs");
  });

  it("writes large counts and amounts compact, the exact line on hover (#10770)", () => {
    const large = figures({
      runs: 1200,
      reviewed: 1200,
      unreviewed: 0,
      range: undefined,
      computation: [
        { kind: "saving", unit: "t", name: "Threat report triaged", units: 47040, manual_min: 940800, review_min: 141120, minutes_saved: 799680, rate: 88.73, value: 1182593.44, currency: "USD" },
        { kind: "total", minutes_saved: 799680, hours_saved: 13328, value: 1182593.44, llm_cost: 0, net_value: 1182593.44, currency: "USD" },
        { kind: "review", reviewed: 1200, total: 1200 },
        { kind: "rejected", count: 1 },
      ],
    });
    renderWithProviders(<RoiKpiCards figures={large} currency="USD" window="30d" />);
    const hours = plain(screen.getByTestId("roi-figure-hours-caption").textContent);
    expect(hours).toBe("1.2K runs");
    expect(screen.getByTitle("1,200 runs")).toBeTruthy();
    expect(plain(screen.getByTestId("roi-figure-review_rate-caption").textContent)).toBe("Reviewed 1.2K of 1.2K records (optional)");
    // The popover's counted line is compact too.
    fireEvent.click(screen.getByTestId("roi-how-hours"));
    expect(screen.getByTestId("roi-how-popover").textContent).toContain("Counted: 42 units from 1.2K runs");
  });

  it("writes the computation lines compact, each exact on hover (#10770)", () => {
    renderWithProviders(
      <RoiAssumptions
        facts={[]}
        computation={[
          { kind: "saving", unit: "t", name: "Threat report triaged", units: 47040, manual_min: 940800, review_min: 141120, minutes_saved: 799680, rate: 88.73, value: 1182593.44, currency: "USD" },
          { kind: "total", minutes_saved: 799680, hours_saved: 13328, value: 1182593.44, llm_cost: 0, net_value: 1182593.44, currency: "USD" },
          { kind: "review", reviewed: 1200, total: 1200 },
          { kind: "rejected", count: 1 },
        ]}
      />,
    );
    const lines = screen.getByTestId("roi-computation").querySelectorAll("li");
    const text = [...lines].map((li) => plain(li.textContent));
    expect(text[0]).toBe("Threat report triaged: 47K units, 941K manual min minus 141K review min = 800K min saved, at $88.73 per hour = $1.2M");
    expect(text[1]).toBe("Hours saved: 800K min = 13.3K h. Net value: $1.2M minus model cost $0 = $1.2M");
    expect(text[2]).toBe("Review rate: 1.2K reviewed of 1.2K records");
    expect(text[3]).toBe("1 rejected run counts no time saved, only its cost.");
    const exact = [...lines].map((li) => plain(li.querySelector("span")?.getAttribute("title") ?? ""));
    expect(exact[0]).toBe("Threat report triaged: 47,040 units, 940,800 manual min minus 141,120 review min = 799,680 min saved, at $88.73 per hour = $1,182,593.44");
    expect(exact[1]).toBe("Hours saved: 799,680 min = 13,328 h. Net value: $1,182,593.44 minus model cost $0.00 = $1,182,593.44");
    expect(exact[2]).toBe("Review rate: 1,200 reviewed of 1,200 records");
    // A line with nothing compacted carries no hover.
    expect(exact[3]).toBe("");
  });

  it("splits the records once for every surface", () => {
    expect(roiRecordCounts({ runs: 12, work_items: 1 })).toEqual({ runs: 11, workItems: 1 });
    expect(roiRecordCounts({ runs: 5 })).toEqual({ runs: 5, workItems: 0 });
    expect(roiComputation(figures({ runs: 12, work_items: 1 }), false)).toMatchObject({ runs: 11, workItems: 1 });
  });
});
