// The drawing of a widget's data with its visualization's options: the single
// value family, the gauges and the table, as a viewer meets them. (The recharts
// charts measure their container, which happy-dom does not lay out: they are
// covered by the pure modules' tests and by screenshots.)

import { describe, expect, it } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { WidgetChartData, WidgetData } from "@/types";

import type { PlatformView } from "../spec";
import PlatformChart from "./PlatformChart";

type Ok = Extract<WidgetData, { status: "ok" }> & WidgetChartData;

const SERIES: Ok = {
  status: "ok",
  shape: "series",
  data: { series: [{ name: "alerts", points: [{ x: "2026-01-01", y: 80 }, { x: "2026-01-02", y: 100 }, { x: "2026-01-03", y: 120 }] }] },
  fetched_at: "2026-10-23T10:00:00Z",
  open_url: null,
};

function draw(data: Ok, view: PlatformView, options: Record<string, unknown> = {}, context: Record<string, unknown> = {}) {
  return renderWithProviders(<PlatformChart data={data} view={view} height={240} options={options} context={context} />);
}

describe("PlatformChart", () => {
  it("draws a single value from a series: its last point, its delta, a sparkline", () => {
    draw(SERIES, "number", { underLabel: "alerts today", unit: "pts" });
    const value = screen.getByTestId("single-value");
    expect(within(value).getByText("120")).toBeTruthy();
    expect(within(value).getByText("pts")).toBeTruthy();
    expect(within(value).getByText("20")).toBeTruthy();
    expect(within(value).getByText("alerts today")).toBeTruthy();
    expect(within(value).getByRole("img", { name: "Trend over the period" })).toBeTruthy();
  });

  it("formats the value and its trend as asked, and colours them dynamically", () => {
    draw(
      SERIES,
      "number",
      {
        trendDisplay: "percent",
        numberPrecision: 1,
        shouldAbbreviateValue: false,
        sparklineDisplay: "off",
        majorColor: "> majorValue | rangeValue(ranges)",
      },
      { ranges: [{ to: 100, value: "success" }, { from: 100, value: "danger" }] },
    );
    const value = screen.getByTestId("single-value");
    const major = within(value).getByText("120.0");
    expect(major.closest("span[style]")?.getAttribute("style")).toContain("var(--color-feedback-error-primary)");
    expect(within(value).getByText((_c, node) => node?.textContent === "20.0%" && node.tagName === "SPAN")).toBeTruthy();
    expect(within(value).queryByRole("img")).toBeNull();
  });

  it("draws a number source's value and change", () => {
    draw({ ...SERIES, shape: "number", data: { value: 1234, change: -12 } }, "number");
    const value = screen.getByTestId("single-value");
    expect(within(value).getByText("1.2K")).toBeTruthy();
    expect(within(value).getByText("12")).toBeTruthy();
  });

  it("draws a radial value as a share of its maximum", () => {
    draw({ ...SERIES, shape: "number", data: { value: 40, change: null } }, "radial", { maxValue: 80 });
    const radial = screen.getByTestId("single-value-radial");
    expect(within(radial).getByRole("img", { name: "40" })).toBeTruthy();
  });

  it("draws the gauges with their value, ranges and ticks", () => {
    draw({ ...SERIES, shape: "number", data: { value: 70, change: null } }, "marker", {
      gaugeRanges: [{ from: 0, to: 50, value: "success" }, { from: 50, to: 200, value: "danger" }],
      majorTickInterval: 50,
    });
    const meter = screen.getByRole("meter");
    expect([meter.getAttribute("aria-valuemin"), meter.getAttribute("aria-valuemax"), meter.getAttribute("aria-valuenow")]).toEqual(["0", "200", "70"]);
    expect(within(meter).getByTestId("gauge-marker")).toBeTruthy();
    expect(["0", "50", "100", "150", "200"].every((tick) => within(meter).getAllByText(tick).length >= 1)).toBe(true);
  });

  it("draws a filler gauge in percent", () => {
    draw({ ...SERIES, shape: "number", data: { value: 30, change: null } }, "filler", { maxValue: 60, valueDisplay: "percentage", labelDisplay: "off" });
    expect(within(screen.getByTestId("gauge-filler")).getByText("50%")).toBeTruthy();
  });

  const ROWS: Ok = {
    status: "ok",
    shape: "rows",
    data: {
      columns: [{ key: "name", label: "Name" }, { key: "count", label: "Count" }, { key: "tags", label: "Tags" }],
      rows: Array.from({ length: 12 }, (_v, i) => ({
        name: `host-${i + 1}`,
        count: String(i + 1),
        tags: i === 0 ? "T1059, T1003" : "",
        ...(i === 0 ? { _url: "javascript:alert(1)" } : {}),
      })),
      total: null,
    },
    fetched_at: "2026-10-23T10:00:00Z",
    open_url: null,
  };

  it("draws a table with paging, row numbers, totals, list cells and cell colours", () => {
    draw(ROWS, "table", {
      count: 5,
      showRowNumbers: true,
      showTotals: true,
      columnFormat: {
        tags: { cellType: "array" },
        count: { align: "right", rowBackgroundColors: "> table | seriesByName('count') | rangeValue(hot)" },
      },
    }, { hot: [{ from: 3, value: "danger" }] });
    expect(screen.getByText("T1059")).toBeTruthy();
    expect(screen.getByText("T1003")).toBeTruthy();
    // A row link that is not http(s) is no link.
    expect(screen.queryByRole("link", { name: "host-1" })).toBeNull();
    expect(screen.getByText("Total")).toBeTruthy();
    expect(screen.getByText("78")).toBeTruthy();
    const third = screen.getByText("3", { selector: "td:not(:first-child)" });
    expect(third.getAttribute("style")).toContain("var(--color-feedback-error-primary)");
    expect(screen.getByText("host-5")).toBeTruthy();
    expect(screen.queryByText("host-6")).toBeNull();
    fireEvent.click(screen.getAllByRole("button").find((b) => b.getAttribute("aria-label")?.toLowerCase().includes("next"))!);
    expect(screen.getByText("host-6")).toBeTruthy();
    expect(screen.getByText("6", { selector: "td:first-child" })).toBeTruthy();
  });

  it("hides the table header when asked", () => {
    draw(ROWS, "table", { headerVisibility: "none" });
    expect(screen.queryByRole("columnheader")).toBeNull();
  });
});
