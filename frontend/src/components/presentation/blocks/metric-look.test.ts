/**
 * Unit tests for what a KPI tile draws - `yarn test:unit`.
 *
 * Pinned here: a `plain` tile is its value and its label and nothing else,
 * whatever the tile carries for the card - so the stage, the editable PPTX
 * and the layout check (both read the rendered DOM) see the same tile - and a
 * card draws every adornment it carries, as it always did.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import type { DeckMetricItem } from "@/types";

import { PLAIN_TILE_UNDRAWN, tileAdornments } from "./metric-look.ts";

const adorned: DeckMetricItem = {
  value: "80K",
  label: "Employees",
  sublabel: "since 2019",
  icon: "users",
  delta: "+4%",
  trend: "up",
  sparkline: [1, 2, 3],
};

const drawn = (metric: DeckMetricItem, variant?: "card" | "plain") =>
  Object.entries(tileAdornments(metric, variant))
    .filter(([, value]) => value !== undefined)
    .map(([key]) => key)
    .sort();

test("a card draws every adornment it carries, and only those", () => {
  assert.deepEqual(drawn(adorned), ["delta", "icon", "sparkline", "sublabel"]);
  assert.deepEqual(drawn(adorned, "card"), ["delta", "icon", "sparkline", "sublabel"]);
  assert.deepEqual(tileAdornments(adorned, "card").sparkline, [1, 2, 3]);
  assert.deepEqual(drawn({ value: "1", label: "One", delta: "+1" }, "card"), ["delta"]);
});

test("a plain tile is its value and its label, whatever it carries for the card", () => {
  assert.deepEqual(tileAdornments(adorned, "plain"), {});
});

test("the fields a plain tile keeps without drawing are every adornment a tile can carry", () => {
  assert.deepEqual([...PLAIN_TILE_UNDRAWN].sort(), ["delta", "delta_tone", "icon", "sparkline", "sublabel", "trend"]);
});
