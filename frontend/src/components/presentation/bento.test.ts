/**
 * Unit tests for the bento placement and the slide
 * palette attribute: the first image is the large cell, the other cells fill
 * the right in reading order, and anything that is no bento lays out as a flow.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { bentoLayout } from "./bento.ts";
import { slidePalette, slidePaletteAttributes, slidePaletteStyle } from "./slide-palette.ts";

const image = { type: "image" } as const;
const text = { type: "text" } as const;

test("three cells: the image on the left over both rows, two cells stacked on the right", () => {
  const layout = bentoLayout([text, image, text]);
  assert.ok(layout);
  assert.equal(layout.columns, "2fr 1fr");
  assert.deepEqual(
    layout.cells.map((cell) => [cell.index, cell.role, cell.area]),
    [
      [1, "big", "1 / 1 / 3 / 2"],
      [0, "cell", "1 / 2 / 2 / 3"],
      [2, "cell", "2 / 2 / 3 / 3"],
    ],
  );
});

test("four cells: two on top, one wide cell under them", () => {
  const layout = bentoLayout([image, text, text, image]);
  assert.ok(layout);
  assert.equal(layout.columns, "2fr 1fr 1fr");
  assert.deepEqual(
    layout.cells.map((cell) => cell.role),
    ["big", "cell", "cell", "wide"],
  );
  assert.equal(layout.cells[3].area, "2 / 2 / 3 / 4");
});

test("five cells: a two by two grid on the right", () => {
  const layout = bentoLayout([image, text, text, text, text]);
  assert.ok(layout);
  assert.deepEqual(
    layout.cells.slice(1).map((cell) => cell.area),
    ["1 / 2 / 2 / 3", "1 / 3 / 2 / 4", "2 / 2 / 3 / 3", "2 / 3 / 3 / 4"],
  );
});

test("no image, too few or too many blocks: no bento", () => {
  assert.equal(bentoLayout([text, text, text]), null);
  assert.equal(bentoLayout([image, text]), null);
  assert.equal(bentoLayout([image, text, text, text, text, text]), null);
});

test("the palette attribute is published only for a known palette", () => {
  assert.deepEqual(slidePaletteAttributes({ palette: "xtm_one" }), { "data-palette": "xtm_one" });
  assert.deepEqual(slidePaletteAttributes({}), {});
  assert.equal(slidePalette({ palette: "violet" as never }), undefined);
});

// The template palette is inline on the slide root, so
// the slide palette must be inline too, merged after it, to win.
test("the palette tokens are inline, per scheme, and win over the template palette when spread after it", () => {
  const template = { "--deck-accent": "#0fbcff", "--deck-chart-1": "#0fbcff", "--deck-bg": "#0a0c1c" };
  const dark = { ...template, ...slidePaletteStyle({ palette: "xtm_one" }, "dark") } as Record<string, string>;
  const light = { ...template, ...slidePaletteStyle({ palette: "xtm_one" }, "light") } as Record<string, string>;
  assert.equal(dark["--deck-accent"], "#9575cd");
  assert.equal(light["--deck-accent"], "#5e1ad5");
  assert.equal(dark["--deck-chart-1"], "#9575cd");
  assert.equal(light["--deck-chart-6"], "#4527a0");
  assert.equal(dark["--deck-gradient-from"], "#9575cd");
  // The rest of the template palette is kept.
  assert.equal(dark["--deck-bg"], "#0a0c1c");
  // A slide in the template's palette publishes nothing.
  assert.deepEqual(slidePaletteStyle({}, "dark"), {});
});
