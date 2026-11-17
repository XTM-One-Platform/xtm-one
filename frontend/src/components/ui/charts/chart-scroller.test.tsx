// The scroller of a chart wider than its card: a named, keyboard-reachable
// region, the Y axis outside it, and the latest data in view on open and after a change.

import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { ChartScroller } from "@/components/ui/charts/chart-scroller";

function setup(resetKey: string) {
  return (
    <ChartScroller
      label="Usage over time chart, scrolls sideways"
      resetKey={resetKey}
      minWidth={4800}
      heightClassName="h-[300px]"
      axis={<span>axis</span>}
      testId="scroller"
    >
      <span>plot</span>
    </ChartScroller>
  );
}

/** jsdom lays nothing out: give the scroller a width to scroll to. */
function stubScrollWidth(value: number) {
  Object.defineProperty(HTMLElement.prototype, "scrollWidth", { configurable: true, get: () => value });
}

describe("ChartScroller", () => {
  afterEach(() => {
    delete (HTMLElement.prototype as { scrollWidth?: number }).scrollWidth;
  });

  it("is a named region reachable by Tab, with the axis outside the scroller", () => {
    render(setup("a"));
    const region = screen.getByRole("region", { name: "Usage over time chart, scrolls sideways" });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region).toHaveClass("overflow-x-auto");
    const axis = screen.getByTestId("scroller-axis");
    expect(region.contains(axis)).toBe(false);
    expect(axis).toHaveAttribute("aria-hidden");
    expect(axis).toHaveClass("order-first");
    expect(region).toContainElement(screen.getByText("plot"));
  });

  it("opens on its right end and goes back there when the key changes", () => {
    stubScrollWidth(4800);
    const { rerender } = render(setup("day"));
    const region = screen.getByRole("region");
    expect(region.scrollLeft).toBe(4800);

    region.scrollLeft = 120;
    rerender(setup("day"));
    expect(region.scrollLeft).toBe(120);

    rerender(setup("week"));
    expect(region.scrollLeft).toBe(4800);
  });

  it("reports the plot width so the figures are laid out from the real room", () => {
    const rect = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ width: 1180.4 } as DOMRect);
    const onPlotWidth = vi.fn();
    render(
      <ChartScroller label="Chart" resetKey="a" minWidth={0} heightClassName="h-[300px]" axis={null} onPlotWidth={onPlotWidth}>
        <span>plot</span>
      </ChartScroller>,
    );
    expect(onPlotWidth).toHaveBeenCalledWith(1180);
    rect.mockRestore();
  });
});
