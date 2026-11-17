// An explicit request to fill the body wins over the hug-content default:
// `vertical_align: stretch` (or equal height with a `fill` card) shares the
// BODY between the rows, a `distribute` run made only of cards, timelines or
// grids reaches the body's floor with no readable-gap cap, and a `fill`
// table in a dashboard panel spreads its rows to the panel's floor. A slide
// that asks for none of this keeps hugging its content.
//
// happy-dom lays nothing out: what is pinned here is what the run publishes
// (`data-spread-free`, no `data-spread-cap`) and the stylesheet's rules that
// turn those requests into the body's height. The geometry itself is read on
// a live deck by `tests_e2e/presentation-distribute.spec.ts`.

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckBlock } from "@/types";
import { BlockList } from "./index";

const stylesheet = readFileSync(`${__dirname}/../presentation.css`, "utf8");

const timeline: DeckBlock = {
  type: "timeline",
  items: [
    { label: "Q1", title: "Plan" },
    { label: "Q2", title: "Build" },
  ],
} as DeckBlock;

const cards: DeckBlock = {
  type: "numbered_cards",
  items: [{ title: "One" }, { title: "Two" }, { title: "Three" }],
} as DeckBlock;

const text = (content: string): DeckBlock => ({ type: "text", content }) as DeckBlock;

function renderRun(blocks: DeckBlock[], distribute: boolean): HTMLElement {
  const { container } = renderWithProviders(
    <div className="deck-root">
      <BlockList blocks={blocks} className="deck-blocks deck-blocks-slide" distribute={distribute} path="blocks" />
    </div>,
  );
  const run = container.querySelector<HTMLElement>(".deck-blocks-slide");
  expect(run).not.toBeNull();
  return run as HTMLElement;
}

describe("a distribute run", () => {
  it("of cards, timelines or grids spreads free: no readable-gap cap", () => {
    const run = renderRun([timeline, cards], true);
    expect(run.dataset.distribute).toBe("true");
    expect(run.dataset.spreadFree).toBe("true");
    expect(run.dataset.spreadCap).toBeUndefined();
  });

  it("holding text stays bounded", () => {
    const run = renderRun([text("First."), timeline], true);
    expect(run.dataset.distribute).toBe("true");
    expect(run.dataset.spreadFree).toBeUndefined();
  });

  it("of columns holding cards or grids spreads free, a heading over them allowed", () => {
    const columns = {
      type: "columns",
      columns: [
        [{ type: "heading", content: "Left", level: 3 }, cards],
        [{ type: "heading", content: "Right", level: 3 }, cards],
      ],
    } as DeckBlock;
    const run = renderRun([timeline, columns], true);
    expect(run.dataset.spreadFree).toBe("true");
    expect(run.dataset.spreadCap).toBeUndefined();
  });

  it("without distribute publishes nothing", () => {
    const run = renderRun([timeline, cards], false);
    expect(run.dataset.distribute).toBeUndefined();
    expect(run.dataset.spreadFree).toBeUndefined();
  });
});

describe("the stylesheet honours an explicit request to fill the body", () => {
  it("shares the body between the rows under stretch, equal rows included", () => {
    expect(stylesheet).toMatch(/\.deck-columns\[data-valign="stretch"\],[^{]*\{\s*align-content: stretch;/);
    expect(stylesheet).toMatch(
      /\.deck-columns\[data-equal-rows="true"\]\[data-valign="stretch"\]:not\(\[data-wrapped\]\),[^{]*\{\s*align-content: stretch;/,
    );
  });

  it("keeps a distributed row of single cards at the body's height", () => {
    expect(stylesheet).toContain(':not(:has(> .deck-column > .deck-blocks[data-spread-free="true"]))');
  });

  it("lifts the template's block gap cap off a run that spreads free", () => {
    expect(stylesheet).toMatch(
      /\[data-design\] \.deck-blocks\[data-spread-free="true"\] \{\s*gap: var\(--deck-block-gap, var\(--deck-space-4\)\);/,
    );
  });

  it("hands a stretched columns block holding a fill table the run's free height", () => {
    expect(stylesheet).toMatch(
      /\.deck-blocks > \.deck-columns:is\(\[data-valign="stretch"\], \[data-equal-height="true"\]\)[^{]*\.deck-table-block\[data-fill="true"\]\) \{\s*flex: 1 1 auto;\s*min-height: 0;\s*align-content: stretch;/,
    );
  });

  it("spreads a fill table's rows in a dashboard panel", () => {
    expect(stylesheet).toContain(
      ':is(.deck-blocks, .deck-panel-body) > .deck-table-block[data-fill="true"] .deck-table { flex: 1 1 auto; }',
    );
  });
});
