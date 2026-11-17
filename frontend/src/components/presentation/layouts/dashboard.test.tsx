// The `dashboard` layout's panel grid: three columns, or two equal ones with
// `panel_columns: 2`, published as `data-columns`.
//
// The real stylesheet and the real component. happy-dom computes the cascade
// but lays nothing out, so what is read here is what the grid is told to
// draw - its tracks and each panel's placement - not pixels; the e2e spec
// (`presentation-charts.spec.ts`) measures those.

import { readFileSync } from "node:fs";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DashboardSlide, DeckDashboardPanel } from "@/types";
import { DashboardLayout } from "./dashboard";

// The icon catalog, loaded: the lazy chunk the deck fetches is ~1,700 ESM
// modules vitest would transform one by one, while the package's own entry
// (loaded natively) exports the same map as `icons`.
vi.mock("../deck-icons", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../deck-icons")>();
  const { icons } = await import("lucide-react");
  return { ...actual, getDeckIconCatalog: () => icons, loadDeckIconCatalog: () => Promise.resolve(icons) };
});

const stylesheet = readFileSync(`${__dirname}/../presentation.css`, "utf8");

beforeAll(() => {
  const style = document.createElement("style");
  style.textContent = stylesheet;
  document.head.appendChild(style);
});

const TILES = ["Instances", "EE customers", "EE trials", "Stars", "Academy", "Partners"].map((label, index) => ({
  value: String(100 + index),
  label,
}));

/** A panel of `span`, absent when 1 is meant by leaving it out. */
const panel = (span?: 1 | 2 | 3): DeckDashboardPanel => ({
  title: `Span ${span ?? "unset"}`,
  ...(span ? { span } : {}),
  block: { type: "metric", value: "42", label: "Figure" },
});

const slideOf = (panels: DeckDashboardPanel[], extra: Partial<DashboardSlide> = {}): DashboardSlide => ({
  layout: "dashboard",
  title: "Adoption",
  tiles: TILES,
  panels,
  ...extra,
});

/** What the grid is told to draw: its rows and tracks, and where each panel goes. */
function grid(slide: DashboardSlide) {
  const { container } = renderWithProviders(
    <div className="deck-root">
      <DashboardLayout slide={slide} />
    </div>,
  );
  const dashboard = container.querySelector<HTMLElement>(".deck-dashboard") as HTMLElement;
  const panels = container.querySelector<HTMLElement>(".deck-dashboard-panels") as HTMLElement;
  const style = getComputedStyle(panels);
  return {
    rows: dashboard.dataset.rows,
    columns: style.gridTemplateColumns,
    rowTracks: style.gridTemplateRows,
    panels: Array.from(panels.querySelectorAll<HTMLElement>("article.deck-panel")).map((article) => ({
      span: article.dataset.span,
      gridColumn: getComputedStyle(article).gridColumn,
    })),
  };
}

/** Today's slides: every span the 3-column grid takes, on one row and on two. */
const THREE_COLUMN_SLIDES: Record<string, DeckDashboardPanel[]> = {
  "six single panels": Array.from({ length: 6 }, () => panel()),
  "spans 3, 1, 2": [panel(3), panel(1), panel(2)],
  "spans 2 and 1 on one row": [panel(2), panel()],
  "two full-width panels": [panel(3), panel(3)],
  "one panel": [panel()],
};

/** The `data-columns` the panel grid publishes. */
function publishedColumns(slide: DashboardSlide): string | undefined {
  const { container } = renderWithProviders(
    <div className="deck-root">
      <DashboardLayout slide={slide} />
    </div>,
  );
  return container.querySelector<HTMLElement>(".deck-dashboard-panels")?.dataset.columns;
}

describe("dashboard panel grid", () => {
  // Recorded on the renderer before `panel_columns` existed: a slide that
  // does not set it must keep drawing this grid.
  it.each(Object.entries(THREE_COLUMN_SLIDES))("draws %s on three columns as it always has", (_name, panels) => {
    expect(grid(slideOf(panels))).toMatchSnapshot();
  });

  it("publishes its columns, 3 when the slide does not say, and draws an explicit 3 as the default", () => {
    const panels = THREE_COLUMN_SLIDES["spans 3, 1, 2"];
    expect(publishedColumns(slideOf(panels))).toBe("3");
    expect(publishedColumns(slideOf(panels, { panel_columns: 3 }))).toBe("3");
    expect(grid(slideOf(panels, { panel_columns: 3 }))).toEqual(grid(slideOf(panels)));
  });

  it("draws panel_columns 2 on two equal columns, two single panels side by side on one row", () => {
    const slide = slideOf([panel(), panel(1)], { panel_columns: 2 });
    expect(publishedColumns(slide)).toBe("2");
    expect(grid(slide)).toEqual({
      rows: "1",
      columns: "repeat(2, minmax(0, 1fr))",
      rowTracks: "minmax(0, 1fr)",
      panels: [
        { span: "1", gridColumn: "" },
        { span: "1", gridColumn: "" },
      ],
    });
  });

  it("places panels on the two columns the way the validator counts them", () => {
    // A full-width panel over two halves: two rows, as `_grid_rows(spans, 2)` says.
    expect(grid(slideOf([panel(2), panel(), panel()], { panel_columns: 2 }))).toMatchObject({
      rows: "2",
      rowTracks: "repeat(2, minmax(0, 1fr))",
      panels: [
        { span: "2", gridColumn: "span 2" },
        { span: "1", gridColumn: "" },
        { span: "1", gridColumn: "" },
      ],
    });
    // Four single panels fill the two rows.
    expect(grid(slideOf(Array.from({ length: 4 }, () => panel()), { panel_columns: 2 })).rows).toBe("2");
    // Three single panels take the same two rows on two columns, one on three.
    const three = Array.from({ length: 3 }, () => panel());
    expect(grid(slideOf(three, { panel_columns: 2 })).rows).toBe("2");
    expect(grid(slideOf(three)).rows).toBe("1");
  });

  it("never asks the two-column grid for a third column, even from a draft the validator would refuse", () => {
    expect(grid(slideOf([panel(3), panel()], { panel_columns: 2 }))).toMatchObject({
      rows: "2",
      panels: [
        { span: "2", gridColumn: "span 2" },
        { span: "1", gridColumn: "" },
      ],
    });
  });
});
