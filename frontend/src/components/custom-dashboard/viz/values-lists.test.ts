/**
 * The values, lists, cards, timelines and maps of the engine, checked against
 * the modules' own numbers (`dev-docs/dashboard-rendering-reference.md`) -
 * `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import type { WidgetGeoData, WidgetRowsData } from "@/types";

import { threatColor } from "../charts/palettes.ts";
import { alternateEntries, cutText, readTimelineStyle, SECONDARY_CHARS } from "./alternate.ts";
import { CONTRAST_BASEMAP, LEVEL_FILL_OPACITY, mercatorFrame, readMapLook, TILE_SIZE, viewCenter } from "./geo.ts";
import {
  ARROW_COLUMN,
  cardOf,
  cardRows,
  chipWidth,
  columnPercent,
  fitChips,
  hasIconColumn,
  ICON_COLUMN,
  MIN_COLUMN_PERCENT,
  readDistributionStyle,
  readListOptions,
  TABLE_HEADER,
} from "./rows.ts";
import { rowsToEvents } from "./shapes.ts";
import { chipStyle, directionOf, readValueStyle, tileStyle, trendLabel, wholeDifference } from "./value.ts";

// ── Values ──────────────────────────────────────────────────────────────────

test("a value's style is one of three, standard unless asked", () => {
  assert.equal(readValueStyle({}), "standard");
  assert.equal(readValueStyle({ valueStyle: "difference" }), "difference");
  assert.equal(readValueStyle({ valueStyle: "tile" }), "tile");
  assert.equal(readValueStyle({ valueStyle: "huge" }), "standard");
});

test("a difference is written whole with its sign, its arrow by its direction", () => {
  assert.deepEqual([directionOf(3), directionOf(-2), directionOf(0), directionOf(null)], ["up", "down", "flat", "flat"]);
  assert.deepEqual([wholeDifference(3), wholeDifference(-2), wholeDifference(0), wholeDifference(1234)], ["3", "-2", "0", "1234"]);
});

test("a trend chip says +N / -N in compact form, no sign at zero", () => {
  assert.equal(trendLabel(100), "+100");
  assert.equal(trendLabel(1500), "+1.5K");
  assert.equal(trendLabel(-2_300_000), "-2.3M");
  assert.equal(trendLabel(0), "0");
});

test("a tile is its colour at 10% with a 13% glow; a chip at 14% within 28%", () => {
  assert.deepEqual(tileStyle("#42caff"), {
    backgroundColor: "color-mix(in srgb, #42caff 10%, transparent)",
    boxShadow: "inset 0 0 14px color-mix(in srgb, #42caff 13%, transparent)",
  });
  assert.deepEqual(chipStyle("red"), {
    backgroundColor: "color-mix(in srgb, red 14%, transparent)",
    border: "1px solid color-mix(in srgb, red 28%, transparent)",
    color: "red",
  });
});

// ── Lists and cards ─────────────────────────────────────────────────────────

test("a list's style and its row height", () => {
  assert.deepEqual(readListOptions({}), { listStyle: "compact", rowHeight: 36, cardsPerRow: 3 });
  // A table's rows are 48 px, a rows list's 50, as their modules draw them.
  assert.equal(readListOptions({ listStyle: "table" }).rowHeight, 48);
  assert.equal(readListOptions({ listStyle: "rows" }).rowHeight, 50);
  assert.deepEqual(readListOptions({ listStyle: "cards", cardsPerRow: 9, rowHeight: 2 }), { listStyle: "cards", rowHeight: 32, cardsPerRow: 6 });
  assert.equal(readListOptions({ listStyle: "grid" }).listStyle, "compact");
  // The arrow's colour as named, resolved where it is drawn.
  assert.equal(readListOptions({ listStyle: "table", arrowColor: "accent" }).arrowColor, "accent");
  assert.deepEqual([TABLE_HEADER, ARROW_COLUMN, ICON_COLUMN], [42, 42, 56]);
});

test("a distribution list is bars unless it asks for rows", () => {
  assert.equal(readDistributionStyle({}), "bars");
  assert.equal(readDistributionStyle({ listStyle: "rows" }), "rows");
  assert.equal(readDistributionStyle({ listStyle: "cards" }), "bars");
});

test("each column of a rows list takes an equal share, 10% at least", () => {
  assert.equal(columnPercent(4), 25);
  assert.equal(columnPercent(3), 100 / 3);
  assert.equal(columnPercent(15), MIN_COLUMN_PERCENT);
  assert.equal(columnPercent(0), 100);
});

test("chips fit their cell, the rest counted in a +N chip", () => {
  // 3 chips of 60 px, 8 px apart, take 196 px.
  assert.equal(fitChips([60, 60, 60], 196), 3);
  // In 150 px: two chips (128) leave no room for "+1" (8 + 36); one does (60 + 8 + 36 = 104).
  assert.equal(fitChips([60, 60, 60], 150), 1);
  // A cell too narrow for any still shows the first.
  assert.equal(fitChips([200, 60], 50), 1);
  assert.equal(fitChips([], 100), 0);
  assert.equal(chipWidth("apt"), 3 * 7 + 16);
  assert.equal(chipWidth("Malware", true), 7 * 7 + 16 + 20);
  assert.equal(chipWidth("x".repeat(100)), 160 + 16);
});

const ROWS: WidgetRowsData = {
  columns: [
    { key: "name", label: "Name" },
    { key: "entity_type", label: "Type", kind: "chip" },
    { key: "modified", label: "Updated", kind: "date" },
  ],
  rows: [
    { name: "Emotet", entity_type: { label: "Malware" }, modified: "2026-10-20T08:00:00Z", _icon: "biohazard", _url: "https://cti.example/dashboard/id/m1" },
    { name: "APT28", entity_type: { label: "Intrusion Set" }, modified: null },
  ],
  total: 2,
};

test("a card is a row's name, icon and last update; cards go three a row", () => {
  assert.deepEqual(cardOf(ROWS, ROWS.rows[0]), { title: "Emotet", date: "2026-10-20T08:00:00Z", icon: "biohazard" });
  assert.deepEqual(cardOf(ROWS, ROWS.rows[1]), { title: "APT28", date: null, icon: null });
  assert.deepEqual(cardRows([1, 2, 3, 4, 5, 6, 7], 3), [[1, 2, 3], [4, 5, 6], [7]]);
  assert.equal(hasIconColumn(ROWS.rows), true);
  assert.equal(hasIconColumn([ROWS.rows[1]]), false);
});

// ── Timeline ────────────────────────────────────────────────────────────────

test("an alternate timeline swaps sides, the first card on the right, its second line cut to 150", () => {
  assert.equal(readTimelineStyle({ timelineStyle: "alternate" }), "alternate");
  assert.equal(readTimelineStyle({}), "lanes");
  assert.equal(SECONDARY_CHARS, 150);
  const entries = alternateEntries({
    events: [
      { label: "a", start: "2026-10-03", end: null, lane: null, value: null, secondary: "s".repeat(200), color: "#70B23B", icon: "file-text" },
      { label: "b", start: "2026-10-02", end: null, lane: null, value: null },
      { label: "c", start: "2026-10-01", end: null, lane: null, value: null },
    ],
  });
  assert.deepEqual(entries.map((e) => e.card), ["right", "left", "right"]);
  assert.equal(entries[0].secondary, `${"s".repeat(150)}…`);
  assert.equal(entries[1].secondary, "");
  assert.deepEqual([entries[0].color, entries[0].icon], ["#70B23B", "file-text"]);
  assert.equal(cutText("short"), "short");
});

test("a table read as events keeps each row's link, type, icon, colour and second line", () => {
  const rows: WidgetRowsData = {
    columns: [
      { key: "date", label: "Date", kind: "datetime" },
      { key: "name", label: "Name" },
      { key: "secondary", label: "Description" },
    ],
    rows: [
      { date: "2026-10-02T08:00:00Z", name: "Q3", secondary: "A report", _url: "https://x/1", _entityType: "Report", _icon: "file-text", _color: "#70B23B" },
      { date: "2026-10-01T08:00:00Z", name: "Bare" },
    ],
    total: null,
  };
  assert.deepEqual(rowsToEvents(rows).events, [
    { label: "Q3", start: "2026-10-02T08:00:00Z", end: null, lane: null, value: null, secondary: "A report", link: "https://x/1", entityType: "Report", icon: "file-text", color: "#70B23B" },
    { label: "Bare", start: "2026-10-01T08:00:00Z", end: null, lane: null, value: null },
  ]);
});

// ── Map ─────────────────────────────────────────────────────────────────────

test("a threat map's look: levels, opacities, its basemap and its city markers", () => {
  const look = readMapLook({
    regionColors: "level", basemap: "contrast", markerIcon: "building-2", markerColor: "#d95f00", markerSize: 25,
    zoom: 1, centerLat: 48.8566969, centerLng: 2.3514616, centerOnFirstMarker: true,
  }); // prettier-ignore
  assert.deepEqual(look, {
    regionColors: "level",
    basemap: "contrast",
    markerColor: "#d95f00",
    markerSize: 25,
    markerIcon: "building-2",
    view: { zoom: 1, center: [2.3514616, 48.8566969], onFirstMarker: true },
  });
  assert.deepEqual(LEVEL_FILL_OPACITY, { dark: 0.1, light: 0.5 });
  assert.deepEqual(CONTRAST_BASEMAP.dark, { land: "#1c2f4a", sea: "#0b1422", border: "#0fbcff" });
  assert.deepEqual(CONTRAST_BASEMAP.light, { land: "#dce0fe", sea: "#ffffff", border: "#001bda" });
  // A level 0 region is drawn as level 5, as the threat map draws an unranked one.
  assert.deepEqual([threatColor(0), threatColor(5), threatColor(9)], ["#d95f00", "#d95f00", "#b71c1c"]);
  // What does not validate falls back: no view, the plain look.
  assert.deepEqual(readMapLook({ markerColor: "url(#x)", markerIcon: "<b>", zoom: 40 }), {
    regionColors: "classes", basemap: "surface", markerColor: undefined, markerSize: 14, markerIcon: undefined, view: null,
  }); // prettier-ignore
});

test("a view opens on its first marker when asked, else on its centre", () => {
  const data: WidgetGeoData = { points: [{ label: "Lyon", lat: 45.76, lon: 4.84, value: 2, group: "City" }], regions: [] };
  const view = { zoom: 1, center: [2.35, 48.86] as [number, number], onFirstMarker: true };
  assert.deepEqual(viewCenter(view, data), [4.84, 45.76]);
  assert.deepEqual(viewCenter({ ...view, onFirstMarker: false }, data), [2.35, 48.86]);
  assert.deepEqual(viewCenter(view, { points: [], regions: [] }), [2.35, 48.86]);
});

test("a Web Mercator view of a zoom is a world 512 x 2^zoom px wide, its centre in the box's middle", () => {
  const { scale, translate } = mercatorFrame(1, [0, 0], 800, 400);
  assert.equal(TILE_SIZE, 512);
  assert.equal(scale * 2 * Math.PI, 1024);
  assert.equal(translate[0], 400);
  assert.ok(Math.abs(translate[1] - 200) < 1e-9);
  // Paris at zoom 1: its point lies in the middle of the box.
  const paris = mercatorFrame(1, [2.3514616, 48.8566969], 800, 400);
  const x = paris.scale * ((2.3514616 * Math.PI) / 180) + paris.translate[0];
  const y = -paris.scale * Math.log(Math.tan(Math.PI / 4 + (48.8566969 * Math.PI) / 360)) + paris.translate[1];
  assert.ok(Math.abs(x - 400) < 1e-9 && Math.abs(y - 200) < 1e-9);
});
