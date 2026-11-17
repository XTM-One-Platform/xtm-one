/**
 * The shared filter pill: Escape closes its panel with the focus back on the
 * trigger (UX r2 A4 on #10312). The 12 px badge text (A1) is held by
 * filter-pill-class.test.ts.
 */
import { useRef, useState } from "react";
import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { FilterPillPanel, FilterPillTrigger, useFilterPopover } from "./filter-pill";

function Pill() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useFilterPopover(ref, open, setOpen);
  return (
    <div>
      <div ref={ref}>
        <FilterPillTrigger label="Filters" open={open} count={2} onClick={() => setOpen((v) => !v)} />
        {open && (
          <FilterPillPanel role="group" aria-label="Filters panel">
            <button type="button">Option</button>
          </FilterPillPanel>
        )}
      </div>
      <button type="button">Elsewhere</button>
    </div>
  );
}

describe("FilterPill", () => {
  it("returns the focus to the trigger when Escape closes the panel", () => {
    render(<Pill />);
    const trigger = screen.getByRole("button", { name: /Filters/ });
    fireEvent.click(trigger);
    const option = screen.getByRole("button", { name: "Option" });
    option.focus();
    fireEvent.keyDown(option, { key: "Escape" });
    expect(screen.queryByRole("group", { name: "Filters panel" })).toBeNull();
    expect(document.activeElement).toBe(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
  });

  it("leaves the focus where it is when it was outside the pill", () => {
    render(<Pill />);
    fireEvent.click(screen.getByRole("button", { name: /Filters/ }));
    const elsewhere = screen.getByRole("button", { name: "Elsewhere" });
    elsewhere.focus();
    fireEvent.keyDown(elsewhere, { key: "Escape" });
    expect(screen.queryByRole("group", { name: "Filters panel" })).toBeNull();
    expect(document.activeElement).toBe(elsewhere);
  });

  it("shows the active filter count on the trigger", () => {
    render(<Pill />);
    expect(screen.getByRole("button", { name: /Filters/ }).textContent).toContain("2");
  });
});

// UX r1 B2 on #10402: a pick in the panel re-rendered the toolbar, the pill
// moved left, and the panel kept the right edge measured at open (x -26).
describe("FilterPillPanel alignment", () => {
  function Moving({ tick }: { tick: number }) {
    return (
      <div data-testid="anchor" style={{ position: "relative" }}>
        <FilterPillPanel role="group" aria-label="Panel" data-tick={tick}>
          <span>Content</span>
        </FilterPillPanel>
      </div>
    );
  }

  it("measures its edge again when the pill moves after it opened", () => {
    const width = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth");
    const viewport = Object.getOwnPropertyDescriptor(document.documentElement, "clientWidth");
    let anchorBox = { left: 1000, right: 1100 };
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 300 });
    Object.defineProperty(document.documentElement, "clientWidth", { configurable: true, get: () => 1200 });
    try {
      const { rerender } = render(<Moving tick={0} />);
      const anchor = screen.getByTestId("anchor");
      anchor.getBoundingClientRect = () => ({ ...anchorBox }) as DOMRect;
      rerender(<Moving tick={1} />);
      expect(screen.getByRole("group", { name: "Panel" }).dataset.align).toBe("right");
      anchorBox = { left: 20, right: 120 };
      rerender(<Moving tick={2} />);
      expect(screen.getByRole("group", { name: "Panel" }).dataset.align).toBe("left");
    } finally {
      if (width) Object.defineProperty(HTMLElement.prototype, "offsetWidth", width);
      if (viewport) Object.defineProperty(document.documentElement, "clientWidth", viewport);
      else Reflect.deleteProperty(document.documentElement, "clientWidth");
    }
  });
});
