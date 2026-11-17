/**
 * Unit tests for the inspector's "this changes nothing here" notes on block
 * fields - `yarn test:unit`.
 *
 * Pinned here: a quote's two credits each say the other holds, a
 * picture no credit names is never flagged (it is named on render), and a
 * `plain` tile - a metric, or every tile of a grid - says that its icon,
 * sublabel, delta, trend and delta tone are kept for the card and not drawn, while a card
 * says nothing about them. A logo grid's gap says where it is only a
 * minimum - spread rows, unframed even cells - and nowhere else. A chart's
 * `framed` says, in a dashboard panel, that the panel is its card,
 * and its `title` that the panel titles the chart and the save
 * refuses a second title. Its `value_label_fit` says it has no effect
 * on a chart that draws no bars - a horizontal one is fitted too -
 * and its `value_label_size` on a pie, a donut or a gauge. A
 * butterfly's own fields say so on any other kind, its totals under
 * no legend, and the target and axis range the save refuses on one.
 * Its `region` says so on anything but a map.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import type { DeckBlock, PresentationSlide } from "@/types";

import { blockFieldInactiveReason, blockItemFieldInactiveReason, fieldInactiveReason } from "./field-applicability.ts";

const ADORNMENTS = ["sublabel", "icon", "delta", "trend", "delta_tone"];

test("a chart's frame says, in a dashboard panel only, that the panel is its card", () => {
  const chart = { type: "chart", kind: "bar", series: [{ name: "ARR", values: [1] }], categories: ["Q1"] } as DeckBlock;
  assert.match(blockFieldInactiveReason(chart, "framed", { inPanel: true }) ?? "", /dashboard panel is this chart's card/);
  assert.equal(blockFieldInactiveReason(chart, "framed"), null);
  assert.equal(blockFieldInactiveReason(chart, "fill", { inPanel: true }), null);
});

test("a chart's title says, in a dashboard panel only, that the panel titles it and the save refuses it", () => {
  const chart = { type: "chart", kind: "bar", series: [{ name: "ARR", values: [1] }], categories: ["Q1"] } as DeckBlock;
  assert.match(blockFieldInactiveReason(chart, "title", { inPanel: true }) ?? "", /Not allowed in a dashboard panel.*save refuses/);
  assert.equal(blockFieldInactiveReason(chart, "title"), null);
  assert.equal(blockFieldInactiveReason(chart, "caption", { inPanel: true }), null);
});

test("a chart's legend position says so while the chart draws no legend", () => {
  const lone = { type: "chart", kind: "bar", series: [{ name: "ARR", values: [1] }], categories: ["Q1"] } as DeckBlock;
  assert.match(blockFieldInactiveReason(lone, "legend_position") ?? "", /draws no legend/);
  const gauge = { type: "chart", kind: "gauge", format: "percent", series: [{ name: "NRR", values: [86] }], categories: ["NRR"] };
  assert.match(blockFieldInactiveReason(gauge as DeckBlock, "legend_position", { inPanel: true }) ?? "", /draws no legend/);
  // Two series, a target line, a donut, or the switch itself: a legend is drawn and the position acts.
  const pair = { ...lone, series: [{ name: "A", values: [1] }, { name: "B", values: [2] }] } as DeckBlock;
  assert.equal(blockFieldInactiveReason(pair, "legend_position"), null);
  assert.equal(blockFieldInactiveReason({ ...lone, target: 2 } as DeckBlock, "legend_position"), null);
  assert.equal(blockFieldInactiveReason({ ...lone, kind: "donut" } as DeckBlock, "legend_position"), null);
  assert.equal(blockFieldInactiveReason({ ...lone, show_legend: true } as DeckBlock, "legend_position"), null);
  assert.match(blockFieldInactiveReason({ ...pair, show_legend: false } as DeckBlock, "legend_position") ?? "", /draws no legend/);
});

test("a chart's unit scales say so until they pin a currency axis", () => {
  const euros = { type: "chart", kind: "bar", format: "currency", series: [{ name: "ARR", values: [1] }], categories: ["Q1"] };
  assert.equal(blockFieldInactiveReason(euros as DeckBlock, "unit_scale"), null);
  for (const idle of [{ ...euros, format: "number" }, { ...euros, format: "percent" }, { ...euros, kind: "pie" }]) {
    assert.match(blockFieldInactiveReason(idle as DeckBlock, "unit_scale") ?? "", /number format is Currency/);
  }
  // The right-hand axis' scale needs a series there, read in currency.
  assert.match(blockFieldInactiveReason(euros as DeckBlock, "secondary_unit_scale") ?? "", /reads against the right axis/);
  const right = { name: "ASP", values: [2], axis: "secondary" };
  const counted = { ...euros, kind: "combo", series: [{ ...euros.series[0], kind: "bar" }, { ...right, kind: "line" }] };
  assert.match(
    blockFieldInactiveReason({ ...counted, secondary_format: "number" } as DeckBlock, "secondary_unit_scale") ?? "",
    /reads against the right axis/,
  );
  assert.equal(blockFieldInactiveReason({ ...counted, secondary_format: "currency" } as DeckBlock, "secondary_unit_scale"), null);
});

test("a chart's value label fit says so where the chart draws no bars, and acts on horizontal bars", () => {
  const bars = { type: "chart", kind: "bar", series: [{ name: "ARR", values: [1] }], categories: ["Q1"] } as DeckBlock;
  for (const kind of ["bar", "stacked_bar", "combo"]) {
    assert.equal(blockFieldInactiveReason({ ...bars, kind } as DeckBlock, "value_label_fit"), null, kind);
    assert.equal(blockFieldInactiveReason({ ...bars, kind } as DeckBlock, "value_label_fit", { inPanel: true }), null, kind);
  }
  for (const kind of ["bar", "stacked_bar"]) {
    assert.equal(blockFieldInactiveReason({ ...bars, kind, horizontal: true } as DeckBlock, "value_label_fit"), null, kind);
  }
  for (const kind of ["line", "area", "pie", "donut", "gauge", "waterfall"]) {
    assert.match(blockFieldInactiveReason({ ...bars, kind } as DeckBlock, "value_label_fit") ?? "", /values of the bars/, kind);
  }
});

test("a chart's value text size says so on a kind that is not an axis chart", () => {
  const bars = { type: "chart", kind: "bar", series: [{ name: "ARR", values: [1] }], categories: ["Q1"] } as DeckBlock;
  for (const kind of ["bar", "stacked_bar", "line", "area", "combo", "waterfall", "butterfly"]) {
    assert.equal(blockFieldInactiveReason({ ...bars, kind } as DeckBlock, "value_label_size"), null, kind);
    assert.equal(blockFieldInactiveReason({ ...bars, kind } as DeckBlock, "value_label_size", { inPanel: true }), null, kind);
  }
  assert.equal(blockFieldInactiveReason({ ...bars, horizontal: true } as DeckBlock, "value_label_size"), null);
  for (const kind of ["pie", "donut", "gauge", "map"]) {
    assert.match(blockFieldInactiveReason({ ...bars, kind } as DeckBlock, "value_label_size") ?? "", /sizes the values of a bar/, kind);
  }
});

test("a butterfly's own fields say so on any other kind, its totals without a legend, and its refusals as refusals", () => {
  const pyramid = {
    type: "chart",
    kind: "butterfly",
    series: [
      { name: "Contributors", values: [40, 80] },
      { name: "Managers", values: [6, 0] },
    ],
    categories: ["Lead", "Senior"],
  } as DeckBlock;
  for (const key of ["spine", "symmetric", "show_totals"]) {
    assert.equal(blockFieldInactiveReason(pyramid, key), null, key);
    assert.equal(blockFieldInactiveReason(pyramid, key, { inPanel: true }), null, key);
    for (const kind of ["bar", "stacked_bar", "pie", "waterfall"]) {
      assert.match(blockFieldInactiveReason({ ...pyramid, kind } as DeckBlock, key) ?? "", /butterfly's/, `${kind}.${key}`);
    }
  }
  assert.match(blockFieldInactiveReason({ ...pyramid, show_legend: false } as DeckBlock, "show_totals") ?? "", /draws no legend/);
  assert.match(blockFieldInactiveReason(pyramid, "target") ?? "", /Not allowed on a butterfly.*save refuses/);
  for (const key of ["y_min", "y_max"]) {
    assert.match(blockFieldInactiveReason(pyramid, key) ?? "", /Not allowed on a butterfly.*pinned axis range/, key);
    assert.equal(blockFieldInactiveReason({ ...pyramid, kind: "bar" } as DeckBlock, key), null, key);
  }
  // A butterfly fits no bars' values: that word is a bar chart's.
  assert.match(blockFieldInactiveReason(pyramid, "value_label_fit") ?? "", /values of the bars/);
});

test("a chart's region says it zooms a map only", () => {
  const countries = { type: "chart", kind: "map", series: [{ name: "Customers", values: [1] }], categories: ["FR"] } as DeckBlock;
  assert.equal(blockFieldInactiveReason(countries, "region"), null);
  assert.equal(blockFieldInactiveReason(countries, "region", { inPanel: true }), null);
  for (const kind of ["bar", "pie", "gauge", "waterfall"]) {
    assert.equal(blockFieldInactiveReason({ ...countries, kind } as DeckBlock, "region"), "No effect here: the region zooms a map chart.", kind);
  }
  // A map draws its ramp unless told not to, so its legend's position always acts.
  assert.equal(blockFieldInactiveReason(countries, "legend_position"), null);
  assert.match(blockFieldInactiveReason({ ...countries, format: "currency" } as DeckBlock, "unit_scale") ?? "", /unit scale/);
});

test("a chart's map legend and markers act on a map, its class count on a classed one", () => {
  const countries = { type: "chart", kind: "map", series: [{ name: "Customers", values: [1] }], categories: ["FR"] } as DeckBlock;
  const classed = { ...countries, legend_style: "classes" } as DeckBlock;
  for (const key of ["legend_style", "markers"]) {
    assert.equal(blockFieldInactiveReason(countries, key), null, key);
    assert.equal(blockFieldInactiveReason(countries, key, { inPanel: true }), null, key);
    for (const kind of ["bar", "pie", "butterfly"]) {
      assert.equal(
        blockFieldInactiveReason({ ...countries, kind } as DeckBlock, key),
        "No effect here: this is a map chart's, the chart that colours countries.",
        `${key} on ${kind}`,
      );
    }
  }
  assert.equal(blockFieldInactiveReason(classed, "classes"), null);
  const ramp = "No effect here: this counts the classes of a map whose legend is Classes.";
  assert.equal(blockFieldInactiveReason(countries, "classes"), ramp);
  assert.equal(blockFieldInactiveReason({ ...countries, legend_style: "ramp" } as DeckBlock, "classes"), ramp);
  assert.equal(blockFieldInactiveReason({ ...classed, kind: "bar" } as DeckBlock, "classes"), ramp);
  // A classed map draws its classes unless told not to, so its legend's position acts too.
  assert.equal(blockFieldInactiveReason(classed, "legend_position"), null);
});

test("a plain metric says its adornments are the card's; a card says nothing", () => {
  const plain = { type: "metric", value: "80K", label: "Employees", variant: "plain", delta: "+4%" } as DeckBlock;
  const card = { type: "metric", value: "80K", label: "Employees", delta: "+4%" } as DeckBlock;
  for (const key of ADORNMENTS) {
    assert.match(blockFieldInactiveReason(plain, key) ?? "", /plain tile draws its value and label only/, key);
    assert.equal(blockFieldInactiveReason(card, key), null, key);
  }
  for (const key of ["value", "label", "tone", "variant", "align"]) {
    assert.equal(blockFieldInactiveReason(plain, key), null, key);
  }
});

test("every tile of a plain grid says the same; a card grid and other lists say nothing", () => {
  const grid = { type: "metrics_grid", items: [{ value: "1", label: "One" }] };
  for (const key of ADORNMENTS) {
    assert.match(
      blockItemFieldInactiveReason({ ...grid, variant: "plain" } as DeckBlock, key) ?? "",
      /plain grid draws each tile's value and label only/,
      key,
    );
    assert.equal(blockItemFieldInactiveReason(grid as DeckBlock, key), null, key);
  }
  assert.equal(blockItemFieldInactiveReason({ ...grid, variant: "plain" } as DeckBlock, "label"), null);
  assert.equal(blockItemFieldInactiveReason({ type: "list", items: [{ text: "x" }] } as DeckBlock, "icon"), null);
});

test("a quote picture without a credit naming it is never flagged: it saves and is named on render", () => {
  const unnamed = { type: "quote", content: "q", author: "A", role: "CISO", photo_file_id: "p", logo_file_id: "l" } as DeckBlock;
  assert.equal(blockFieldInactiveReason(unnamed, "photo_file_id"), null);
  assert.equal(blockFieldInactiveReason(unnamed, "logo_file_id"), null);
});

test("a logo grid's gap says when it is only a minimum and stays quiet where it spaces the logos", () => {
  const logos = (count: number) => Array.from({ length: count }, (_, index) => ({ file_id: `logo-${index}` }));
  const grid = (count: number, fields: object = {}) => ({ type: "logo_grid", items: logos(count), ...fields }) as DeckBlock;
  const gap = (block: DeckBlock) => blockFieldInactiveReason(block, "gap");
  // One row spread edge to edge: the free width is shared out, the gap is a floor.
  assert.match(gap(grid(4, { align: "stretch", columns: 4 })) ?? "", /only a minimum/);
  assert.match(gap(grid(5)) ?? "", /only a minimum/, "no columns: stretch by default");
  // Equal cells: the gap is the gutter between framed tiles, a fraction of a move without them.
  assert.equal(gap(grid(4, { columns: 4 })), null, "even and framed by default");
  assert.match(gap(grid(4, { columns: 4, framed: false })) ?? "", /barely moves them/);
  // `start` under `columns` is the same equal cells, packed from the start.
  assert.equal(gap(grid(4, { align: "start", columns: 4 })), null);
  assert.match(gap(grid(4, { align: "start", columns: 4, framed: false })) ?? "", /barely moves them/);
  // Where it acts: packed logos, a short row, rows stacked without fill.
  for (const fields of [{ align: "center", columns: 4 }, { align: "start" }, { align: "stretch", columns: 6 }]) {
    assert.equal(gap(grid(4, fields)), null, JSON.stringify(fields));
  }
  assert.equal(gap(grid(8, { align: "stretch", columns: 4 })), null, "two rows: the gap is between them");
  assert.match(gap(grid(8, { align: "stretch", columns: 4, fill: true })) ?? "", /only a minimum/, "fill spreads the rows");
  assert.match(gap(grid(1)) ?? "", /single logo/);
  // Other fields of the grid never carry the note.
  assert.equal(blockFieldInactiveReason(grid(4, { align: "stretch", columns: 4 }), "align"), null);
});

test("equal row heights are for columns, and columns lined up as rows are not spread", () => {
  const card = { type: "phase_card", title: "A" };
  const flow = { layout: "content", title: "Flow", blocks: [card, card], distribute: true } as unknown as PresentationSlide;
  const columns = {
    layout: "content",
    title: "Columns",
    columns: [
      [card, card],
      [card, card],
    ],
    distribute: true,
  } as unknown as PresentationSlide;
  assert.match(fieldInactiveReason(flow, "equal_rows") ?? "", /equal row heights are for columns/);
  assert.equal(fieldInactiveReason(columns, "equal_rows"), null);
  assert.equal(fieldInactiveReason(columns, "distribute"), null);
  assert.match(fieldInactiveReason({ ...columns, equal_rows: true } as PresentationSlide, "distribute") ?? "", /not spread/);
  // A single flow ignores the switch: its spread is unaffected.
  assert.equal(fieldInactiveReason({ ...flow, equal_rows: true } as PresentationSlide, "distribute"), null);
});

test("a lone card grid says it is centred until the slide places it", () => {
  const grid = { type: "numbered_cards", items: [] };
  const slide = { layout: "content", title: "Grid", blocks: [grid] } as unknown as PresentationSlide;
  assert.match(fieldInactiveReason(slide, "vertical_align") ?? "", /card grid centres it/);
  assert.equal(fieldInactiveReason({ ...slide, vertical_align: "end" } as PresentationSlide, "vertical_align"), null);
  const filled = { ...slide, blocks: [{ ...grid, fill: true }] } as unknown as PresentationSlide;
  assert.equal(fieldInactiveReason(filled, "vertical_align"), null);
});

test("a quote's legacy attribution and its split credit each say the other holds", () => {
  const legacy = { type: "quote", content: "q", attribution: "A, CISO" } as DeckBlock;
  const split = { type: "quote", content: "q", author: "A" } as DeckBlock;
  assert.match(blockFieldInactiveReason(legacy, "author") ?? "", /one-line attribution credits this quote/);
  assert.match(blockFieldInactiveReason(split, "attribution") ?? "", /save refuses both/);
  assert.equal(blockFieldInactiveReason(split, "author"), null);
});
