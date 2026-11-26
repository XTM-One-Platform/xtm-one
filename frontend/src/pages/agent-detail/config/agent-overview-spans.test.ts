/**
 * The agent General read view (#11838): every row of the overview grid is full
 * at every breakpoint, no row holds more than 6 tiles, the wide layout is two
 * rows, and the tiles render in the order the spans were chosen for.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { OVERVIEW_GRID_COLUMNS, resolvedOverviewSpans } from "@/components/ui/overview-grid-spans";

import { AGENT_OVERVIEW_ORDER, AGENT_OVERVIEW_SPANS } from "./agent-overview-spans";

const BREAKPOINTS = ["base", "sm", "md", "lg", "2xl"] as const;

/** Lay the tiles out row by row as CSS grid auto-placement does (no dense packing). */
function rows(bp: (typeof BREAKPOINTS)[number]): number[][] {
  const columns = OVERVIEW_GRID_COLUMNS[bp];
  const out: number[][] = [];
  let row: number[] = [];
  let used = 0;
  for (const key of AGENT_OVERVIEW_ORDER) {
    const span = resolvedOverviewSpans(AGENT_OVERVIEW_SPANS[key])[bp];
    if (used + span > columns) {
      out.push(row);
      row = [];
      used = 0;
    }
    row.push(span);
    used += span;
  }
  out.push(row);
  return out;
}

describe("agent General overview grid (#11838)", () => {
  it.each(BREAKPOINTS)("fills every row at %s, at most 6 tiles a row", (bp) => {
    for (const row of rows(bp)) {
      expect(row.reduce((a, b) => a + b, 0)).toBe(OVERVIEW_GRID_COLUMNS[bp]);
      expect(row.length).toBeLessThanOrEqual(6);
    }
  });

  it("lays two rows on a wide screen and three at lg", () => {
    expect(rows("2xl")).toEqual([
      [2, 2, 2, 2, 2, 2],
      [2, 3, 4, 3],
    ]);
    expect(rows("lg")).toEqual([
      [2, 1, 1, 2],
      [2, 2, 1, 1],
      [4, 2],
    ]);
  });

  it("keeps the read view in the order of the spans", () => {
    const source = readFileSync(join(__dirname, "GeneralSection.tsx"), "utf8");
    const positions = AGENT_OVERVIEW_ORDER.map((key) => {
      const index = source.indexOf(`span={AGENT_OVERVIEW_SPANS.${key}}`);
      expect(index, key).toBeGreaterThan(-1);
      return index;
    });
    expect(positions).toEqual(positions.slice().sort((a, b) => a - b));
  });
});
