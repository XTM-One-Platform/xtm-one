// The polar area as a viewer meets it: a slice per label and what a click on
// one says. (Moved from the validation views' tests when those views were
// replaced; the radar, the word cloud and the heatmap measure their container,
// which happy-dom does not lay out: `polar-charts.test.ts` covers their geometry.)

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { WidgetChartData, WidgetData } from "@/types";

import type { PlatformView } from "../spec";
import PlatformChart from "./PlatformChart";

type Ok = Extract<WidgetData, { status: "ok" }> & WidgetChartData;

const ok = (data: WidgetChartData): Ok => ({ status: "ok", fetched_at: "2026-10-26T10:00:00Z", open_url: null, ...data }) as Ok;

function draw(data: Ok, view: PlatformView, onSelect = vi.fn()) {
  renderWithProviders(<PlatformChart data={data} view={view} height={260} onSelect={onSelect} />);
  return onSelect;
}

describe("the polar area", () => {
  it("draws a slice per label, the largest to the rim, and says which one was clicked", () => {
    const onSelect = draw(
      ok({ shape: "distribution", data: { items: [{ label: "APT28", value: 40 }, { label: "Lazarus", value: 10 }] } }),
      "polar",
    );
    const chart = screen.getByRole("img", { name: "Polar area chart" });
    const slices = chart.querySelectorAll("path");
    expect([...slices].map((slice) => slice.querySelector("title")?.textContent)).toEqual(["APT28: 40", "Lazarus: 10"]);
    expect(slices[0].getAttribute("d")).toBe("M 0 0 L 0 -1 A 1 1 0 0 1 0 1 Z");
    fireEvent.click(slices[1]);
    expect(onSelect.mock.calls).toEqual([[{ name: "Lazarus", value: "Lazarus", y: "10" }]]);
    // The legend names each slice.
    expect(screen.getByText("APT28", { selector: "span" })).toBeTruthy();
  });
});
