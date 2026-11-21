// @vitest-environment node
import { describe, expect, it } from "vitest";
import { CTEM_CASE_STATES } from "../src/hooks/api/useCtemCasesApi";
import { BOARD_GRID, BOARD_GRID_COLUMNS } from "../src/pages/xtm-one/ctem/cases-board";

/**
 * The Cases board draws one column per case state (UX round 1 of #857, B2): at the
 * reference width the grid has as many columns as states, so no state sits alone
 * on a second row. Adding a state updates the grid.
 */
describe("Cases board grid", () => {
  it("has one column per case state at the reference width", () => {
    expect(BOARD_GRID_COLUMNS).toBe(CTEM_CASE_STATES.length);
    expect(BOARD_GRID).toContain(`xl:grid-cols-${CTEM_CASE_STATES.length}`);
  });

  it("never draws an intermediate breakpoint that orphans a column", () => {
    const counts = [...BOARD_GRID.matchAll(/grid-cols-(\d+)/g)].map((m) => Number(m[1]));
    for (const n of counts) {
      expect(n === 1 || CTEM_CASE_STATES.length % n === 0, `grid-cols-${n} leaves an orphan column`).toBe(true);
    }
  });
});
