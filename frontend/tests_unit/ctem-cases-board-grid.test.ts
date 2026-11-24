// @vitest-environment node
import { describe, expect, it } from "vitest";
import { CTEM_CASE_STATES } from "../src/hooks/api/useCtemCasesApi";
import { BOARD_GRID, BOARD_GRID_COLUMNS } from "../src/pages/xtm-one/ctem/cases-board";

/**
 * The Cases board draws one card per case status (UX round 1 of #857, B2): the
 * eight statuses of #1503 sit on full rows (four per row at xl), so no status sits
 * alone on a row. Adding a status updates the grid.
 */
describe("Cases board grid", () => {
  it("has one card per case status, in full rows at the reference width", () => {
    expect(BOARD_GRID_COLUMNS).toBe(CTEM_CASE_STATES.length);
    const xl = Number(/xl:grid-cols-(\d+)/.exec(BOARD_GRID)?.[1]);
    expect(xl).toBeGreaterThanOrEqual(4);
    expect(CTEM_CASE_STATES.length % xl).toBe(0);
  });

  it("never draws an intermediate breakpoint that orphans a column", () => {
    const counts = [...BOARD_GRID.matchAll(/grid-cols-(\d+)/g)].map((m) => Number(m[1]));
    for (const n of counts) {
      expect(n === 1 || CTEM_CASE_STATES.length % n === 0, `grid-cols-${n} leaves an orphan column`).toBe(true);
    }
  });
});
