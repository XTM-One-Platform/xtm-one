import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OverviewGrid, OverviewTile } from "@/components/ui/overview-grid";
import { overviewSpanClasses, resolvedOverviewSpans } from "@/components/ui/overview-grid-spans";

describe("overview grid spans (#11838)", () => {
  it("emits one column class per breakpoint the tile names, clamped to the grid", () => {
    expect(overviewSpanClasses({ sm: 3, lg: 2, "2xl": 3 })).toBe("sm:col-span-2 lg:col-span-2 2xl:col-span-3");
    expect(overviewSpanClasses()).toBe("");
  });

  it("inherits an unnamed breakpoint from the narrower one", () => {
    expect(resolvedOverviewSpans({ md: 3 })).toEqual({ base: 1, sm: 1, md: 3, lg: 3, "2xl": 3 });
    expect(resolvedOverviewSpans({ sm: 2 })).toEqual({ base: 1, sm: 2, md: 2, lg: 2, "2xl": 2 });
  });
});

describe("OverviewTile", () => {
  afterEach(() => vi.restoreAllMocks());

  const renderTile = () =>
    render(
      <OverviewGrid testId="grid">
        <OverviewTile label="Organization context" span={{ "2xl": 3 }} hint="On, from the platform" testId="tile">
          Inherit
        </OverviewTile>
      </OverviewGrid>,
    );

  it("shows the label, the value and the hint inside its grid", () => {
    renderTile();
    const tile = screen.getByTestId("tile");
    expect(screen.getByTestId("grid").contains(tile)).toBe(true);
    for (const text of ["Organization context", "Inherit", "On, from the platform"]) {
      expect(tile.contains(screen.getByText(text))).toBe(true);
    }
  });

  it("cuts the label, a string value and the hint on one line each, each reachable for its tooltip", () => {
    // jsdom lays nothing out: every line is wider than its box, so each one is cut.
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(200);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(80);
    renderTile();
    for (const text of ["Organization context", "Inherit", "On, from the platform"]) {
      expect(screen.getByText(text).tabIndex).toBe(0);
    }
  });
});
