/**
 * Unit tests for the bounded spread maths.
 *
 * Run with `yarn test:unit` - Node's built-in runner, stripping the types
 * natively. The DOM half (`capSpread`, `useSpreadCap`) is exercised on a live
 * deck by `tests_e2e/presentation-fill.spec.ts` and
 * `presentation-distribute.spec.ts`; what is pinned here is the arithmetic
 * both halves rely on: which children form a row, and how far a spread may go.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  SPREAD_AGENDA_ROW_MAX_EXTRA_PX,
  SPREAD_BLOCK_MAX_EXTRA_PX,
  SPREAD_CARD_MAX_EXTRA_PX,
  SPREAD_ROUNDING_SLACK_PX,
  SPREAD_ROW_MAX_EXTRA_PX,
  rowHeights,
  spreadCap,
  spreadsFree,
} from "./spread-math.ts";

test("a distribute run of cards, timelines or grids spreads to the floor, a run holding text stays bounded", () => {
  assert.equal(spreadsFree([{ type: "timeline" }, { type: "numbered_cards" }]), true);
  assert.equal(spreadsFree([{ type: "logo_grid" }, { type: "logo_wall" }]), true);
  assert.equal(spreadsFree([{ type: "text" }, { type: "logo_grid" }]), false);
  assert.equal(spreadsFree([{ type: "callout" }]), true);
  assert.equal(spreadsFree([{ type: "text" }, { type: "list" }, { type: "quote" }]), false);
  assert.equal(spreadsFree([]), false);
  assert.equal(spreadsFree(undefined), false);
});

test("a columns block in a distribute run counts by what it holds", () => {
  const logos = { type: "columns", columns: [[{ type: "heading" }, { type: "logo_grid" }], [{ type: "heading" }, { type: "logo_grid" }]] };
  assert.equal(spreadsFree([logos, logos]), true);
  const cards = { type: "columns", columns: [[{ type: "callout" }], [{ type: "callout" }], [{ type: "callout" }]] };
  assert.equal(spreadsFree([{ type: "timeline" }, cards]), true);
  // A column holding text keeps the run bounded; headings alone spread nothing.
  assert.equal(spreadsFree([{ type: "columns", columns: [[{ type: "text" }], [{ type: "logo_grid" }]] }]), false);
  assert.equal(spreadsFree([{ type: "columns", columns: [[{ type: "heading" }], [{ type: "heading" }]] }]), false);
  assert.equal(spreadsFree([{ type: "columns" }]), false);
  assert.equal(spreadsFree([{ type: "heading" }, { type: "logo_grid" }]), false);
});

test("children sharing a top edge form one row, the tallest one setting its height", () => {
  // A two-column list: items 1+2, 3+4, 5 (the odd last row), out of order.
  const boxes = [
    { top: 200, height: 40 },
    { top: 0, height: 40 },
    { top: 100, height: 40 },
    { top: 0.6, height: 56 },
    { top: 100, height: 39 },
  ];
  assert.deepEqual(rowHeights(boxes), [56, 40, 40]);
});

test("a column of blocks is a row per block", () => {
  const boxes = [
    { top: 0, height: 120 },
    { top: 152, height: 80 },
    { top: 264, height: 200 },
  ];
  assert.deepEqual(rowHeights(boxes), [120, 80, 200]);
});

test("the cap is the natural height plus one extra per gap and the rounding slack", () => {
  // Six one-line bullets at 39px with the list's 16px row gap.
  const rows = [39, 39, 39, 39, 39, 39];
  const natural = 6 * 39 + 5 * 16;
  assert.equal(spreadCap(rows, 16, SPREAD_ROW_MAX_EXTRA_PX), natural + 5 * SPREAD_ROW_MAX_EXTRA_PX + SPREAD_ROUNDING_SLACK_PX);
  // Three blocks at the run's 32px gap.
  assert.equal(
    spreadCap([120, 80, 200], 32, SPREAD_BLOCK_MAX_EXTRA_PX),
    400 + 2 * 32 + 2 * SPREAD_BLOCK_MAX_EXTRA_PX + SPREAD_ROUNDING_SLACK_PX,
  );
});

test("a single row has no gap to grow: the cap is its own height", () => {
  // A framed key_value with one pair and `fill`: the frame must not stretch to the floor.
  assert.equal(spreadCap([64], 0, SPREAD_ROW_MAX_EXTRA_PX, 34), 64 + 34 + SPREAD_ROUNDING_SLACK_PX);
});

test("cards that share the height grow by one extra per row, not per gap", () => {
  // One row of three takeaways, 306px tall: no gap to grow, the row itself
  // takes the extra - the card stops near 400px instead of filling the body.
  assert.equal(
    spreadCap([306], 24, SPREAD_CARD_MAX_EXTRA_PX, 0, "rows"),
    306 + SPREAD_CARD_MAX_EXTRA_PX + SPREAD_ROUNDING_SLACK_PX,
  );
  // Two rows: one extra each, plus the grid's own gap between them.
  assert.equal(
    spreadCap([271, 271], 24, SPREAD_CARD_MAX_EXTRA_PX, 0, "rows"),
    2 * 271 + 24 + 2 * SPREAD_CARD_MAX_EXTRA_PX + SPREAD_ROUNDING_SLACK_PX,
  );
  // Every other spread grows its gaps.
  assert.equal(spreadCap([40, 40], 16, SPREAD_ROW_MAX_EXTRA_PX), spreadCap([40, 40], 16, SPREAD_ROW_MAX_EXTRA_PX, 0, "gaps"));
});

test("a border-box element counts its padding and border inside the cap", () => {
  const bare = spreadCap([40, 40], 16, SPREAD_ROW_MAX_EXTRA_PX);
  const framed = spreadCap([40, 40], 16, SPREAD_ROW_MAX_EXTRA_PX, 26);
  assert.equal(framed, (bare as number) + 26);
});

test("nothing visible means nothing to cap", () => {
  assert.equal(spreadCap([], 16, SPREAD_ROW_MAX_EXTRA_PX), null);
});

test("the rounding a measurement can lose never exceeds what the cap adds", () => {
  // `offsetHeight` rounds each row to a whole pixel, so n rows measure at most
  // n / 2 px short of their true height. The cap must still hold the true
  // content: a single row (no gap to grow) relies on the slack alone, every
  // larger count on the extras it adds per gap - for the row AND the block
  // extra, up to the longest list the validator accepts.
  for (const extra of [SPREAD_ROW_MAX_EXTRA_PX, SPREAD_BLOCK_MAX_EXTRA_PX]) {
    for (let rows = 1; rows <= 16; rows += 1) {
      const measured = Array.from({ length: rows }, () => 39);
      const cap = spreadCap(measured, 0, extra) as number;
      const trueContent = rows * 39.5;
      assert.ok(cap >= trueContent, `${rows} rows at extra ${extra}: cap ${cap} < true ${trueContent}`);
    }
  }
  assert.ok(SPREAD_ROUNDING_SLACK_PX >= 0.5);
});

/**
 * Where a `distribute` agenda's rows land in a body of `room` px: the
 * list takes the room up to its cap (the flex stretch), the rows spread over
 * that height, and what is left past the cap is split above and below the
 * list (its auto block margins). Returns the gap between two rows and the
 * list's top offset in the body.
 */
function agendaPlacement(rows: number[], room: number): { gap: number; top: number } {
  const cap = spreadCap(rows, 24, SPREAD_AGENDA_ROW_MAX_EXTRA_PX) as number;
  const height = Math.min(room, cap);
  const content = rows.reduce((sum, row) => sum + row, 0);
  return { gap: rows.length > 1 ? (height - content) / (rows.length - 1) : 0, top: (room - height) / 2 };
}

// The 16:9 body of the brand deck at 1080px (796px, measured in the lab) and
// the rows of a titled agenda item with its one-line description (81px; a
// three-column first row whose title wraps, 124px), as the API stores the
// slide: `distribute: true`.
const AGENDA_BODY_PX = 796;

test("two or three agenda rows stop at a readable gap and sit centred as a group", () => {
  // 4 items in 2 columns, list or grid: one readable gap, the pair centred.
  const two = agendaPlacement([81, 81], AGENDA_BODY_PX);
  assert.equal(two.gap, 24 + SPREAD_AGENDA_ROW_MAX_EXTRA_PX + SPREAD_ROUNDING_SLACK_PX);
  assert.ok(two.top > 200, `the pair is centred, top ${two.top}`);
  // 8 items in 3 columns: two readable gaps, the group centred.
  const three = agendaPlacement([124, 81, 81], AGENDA_BODY_PX);
  assert.ok(three.gap <= 24 + SPREAD_AGENDA_ROW_MAX_EXTRA_PX + SPREAD_ROUNDING_SLACK_PX);
  assert.ok(three.top > 50, `the three rows are centred, top ${three.top}`);
});

test("four agenda rows and more fill the body as before", () => {
  // 4 items in 1 column, or 8 in 2 columns: under the cap, the rows reach the floor.
  const four = agendaPlacement([81, 81, 81, 81], AGENDA_BODY_PX);
  assert.equal(four.top, 0);
  assert.equal(four.gap, (AGENDA_BODY_PX - 4 * 81) / 3);
  // 10 items in 2 columns: five rows, fully distributed.
  const five = agendaPlacement([81, 81, 81, 81, 81], AGENDA_BODY_PX);
  assert.equal(five.top, 0);
  assert.equal(five.gap, (AGENDA_BODY_PX - 5 * 81) / 4);
});

test("the extras are canvas pixels a reader forgives, not a share of the room", () => {
  // The whole point: a 16px list gap may reach 64px, a 32px block gap
  // 96px - about 1.6 and 2.5 lines at the body size - and no further, however
  // tall the slide is.
  assert.equal(16 + SPREAD_ROW_MAX_EXTRA_PX, 64);
  assert.equal(32 + SPREAD_BLOCK_MAX_EXTRA_PX, 96);
  assert.ok(SPREAD_ROW_MAX_EXTRA_PX < SPREAD_BLOCK_MAX_EXTRA_PX);
  // A takeaway card grows its poster gap - the room between its number and
  // its title - by 96px at most: a card, never a pillar.
  assert.equal(SPREAD_CARD_MAX_EXTRA_PX, 96);
  // An agenda row gap reaches 160px at most: about two rows of a titled item.
  assert.equal(24 + SPREAD_AGENDA_ROW_MAX_EXTRA_PX, 160);
});
