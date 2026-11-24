/**
 * Unit tests for the custom dashboard reader - `yarn test:unit`.
 *
 * A stored dashboard is read, never trusted: whatever a row holds, the reader
 * answers drawable widgets, a placeholder for the ones it cannot draw, and no
 * link outside the web app.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { isSafeDashboardHref, normalizeDashboard, sparklinePoints, MAX_RENDERED_WIDGETS } from "./spec.ts";

test("a valid dashboard reads as stored, with the defaults filled in", () => {
  const dashboard = normalizeDashboard({
    title: "Today",
    subtitle: "Friday 16 Oct",
    widgets: [
      {
        kind: "score",
        id: "score",
        label: "Exposure score",
        value: 62,
        max: 100,
        trend: { direction: "down", label: "8 pts this week", tone: "success" },
        series: [78, 74, 62],
        stats: [{ label: "Critical", value: "7", tone: "warning" }],
      },
      { kind: "stages", id: "cycle", width: "half", items: [{ label: "Scoping", state: "done" }] },
    ],
  });
  assert.equal(dashboard.title, "Today");
  assert.equal(dashboard.subtitle, "Friday 16 Oct");
  assert.deepEqual(
    dashboard.widgets.map((w) => [w.id, w.kind, w.width]),
    [
      ["score", "score", "full"],
      ["cycle", "stages", "half"],
    ],
  );
  const [score] = dashboard.widgets;
  assert.equal(score.kind, "score");
  if (score.kind === "score") {
    assert.deepEqual(score.trend, { direction: "down", label: "8 pts this week", tone: "success" });
    assert.deepEqual(score.stats, [{ label: "Critical", value: "7", caption: undefined, tone: "warning" }]);
  }
});

test("whatever the row holds, the reader answers and never throws", () => {
  for (const raw of [null, undefined, 42, "dashboard", [], { widgets: "many" }, { widgets: [null, 3, "x", []] }]) {
    const dashboard = normalizeDashboard(raw);
    assert.equal(typeof dashboard.title, "string");
    for (const widget of dashboard.widgets) assert.equal(widget.kind, "unsupported");
  }
});

test("an unknown kind, or a widget missing what it needs, is a placeholder naming its kind", () => {
  const { widgets } = normalizeDashboard({
    title: "T",
    widgets: [
      { kind: "gauge", id: "g", value: 3 },
      { kind: "score", id: "s", label: "No value" },
      { kind: "decision", id: "d", text: "   " },
      { kind: "stats", id: "empty", items: [] },
      { kind: "text", id: "ok", body: "Fine" },
    ],
  });
  assert.deepEqual(
    widgets.map((w) => (w.kind === "unsupported" ? `?${w.originalKind}` : w.kind)),
    ["?gauge", "?score", "?decision", "?stats", "text"],
  );
});

test("tones, widths and stage states outside their lists fall back, never pass through", () => {
  const { widgets } = normalizeDashboard({
    title: "T",
    widgets: [
      { kind: "callout", text: "x", tone: "bg-[red]", width: "huge" },
      { kind: "stages", items: [{ label: "a", state: "exploded", progress: 7 }] },
    ],
  });
  const [callout, stages] = widgets;
  assert.equal(callout.kind === "callout" && callout.tone, "neutral");
  assert.equal(callout.width, "full");
  assert.deepEqual(stages.kind === "stages" && stages.items[0], {
    label: "a",
    state: "pending",
    status: undefined,
    progress: 1,
  });
});

test("ids are unique and DOM-safe, so no two widgets share a key", () => {
  const { widgets } = normalizeDashboard({
    title: "T",
    widgets: [
      { kind: "text", id: "a", body: "1" },
      { kind: "text", id: "a", body: "2" },
      { kind: "text", body: "3" },
      { kind: "text", id: "<script>", body: "4" },
    ],
  });
  const ids = widgets.map((w) => w.id);
  assert.equal(new Set(ids).size, 4);
  assert.deepEqual(ids, ["a", "a-2", "widget-3", "widget-4"]);
});

test("strings and lists are bounded, numbers read as written", () => {
  const { widgets } = normalizeDashboard({
    title: "x".repeat(500),
    widgets: [
      { kind: "stats", items: Array.from({ length: 20 }, (_, i) => ({ label: `L${i}`, value: i * 1.5 })) },
      ...Array.from({ length: MAX_RENDERED_WIDGETS + 10 }, () => ({ kind: "text", body: "b" })),
    ],
  });
  assert.equal(widgets.length, MAX_RENDERED_WIDGETS);
  const [stats] = widgets;
  assert.equal(stats.kind === "stats" && stats.items.length, 6);
  assert.deepEqual(stats.kind === "stats" && stats.items.slice(0, 2).map((s) => s.value), ["0", "1.5"]);
  assert.equal(normalizeDashboard({ title: "x".repeat(500) }).title.length, 120);
});

const SAFE = ["/agents", "/presentations/3f0c?tab=slides#/s1", "/ctem", "/"];
const UNSAFE = [
  "https://evil.example",
  "http://evil.example",
  "//evil.example",
  "/\\evil.example",
  "javascript:alert(1)",
  "JAVASCRIPT:alert(1)",
  "data:text/html,x",
  "/api/v1/users",
  "/API",
  "agents",
  "/agents foo",
  "/agents\nfoo",
  "/agents<script>",
  '/a"b',
  "",
  42,
  null,
];

test("a link is a page of this web app, nothing else", () => {
  for (const href of SAFE) assert.equal(isSafeDashboardHref(href), true, href);
  for (const href of UNSAFE) assert.equal(isSafeDashboardHref(href), false, String(href));
});

test("an action with an unsafe link falls back to its prompt, or is dropped", () => {
  const { widgets } = normalizeDashboard({
    title: "T",
    widgets: [
      {
        kind: "callout",
        text: "x",
        link: { label: "Open", href: "https://evil.example" },
        actions: [
          { label: "Phish", href: "//evil.example" },
          { label: "Ask", href: "javascript:x", prompt: "What changed?" },
          { label: "Fine", href: "/agents" },
        ],
      },
      { kind: "feed", items: [{ title: "x", href: "https://evil.example" }] },
    ],
  });
  const [callout, feed] = widgets;
  assert.equal(callout.link, undefined);
  assert.deepEqual(callout.kind === "callout" && callout.actions, [{ label: "Ask", prompt: "What changed?" }, { label: "Fine", href: "/agents" }]);
  assert.equal(feed.kind === "feed" && feed.items[0].href, undefined);
});

test("a sparkline spans its box, lowest value at the bottom", () => {
  assert.deepEqual(sparklinePoints([5], 100, 40), []);
  assert.deepEqual(sparklinePoints([0, 10], 100, 40, 0), [
    [0, 40],
    [100, 0],
  ]);
  // A flat line sits at the bottom instead of dividing by zero.
  assert.deepEqual(sparklinePoints([3, 3], 10, 10, 0), [
    [0, 10],
    [10, 10],
  ]);
});

test("a stored layout is kept, a missing or off-grid one is placed below in rows by width", () => {
  const { widgets } = normalizeDashboard({
    title: "T",
    widgets: [
      { kind: "text", id: "placed", body: "p", layout: { x: 6, y: 0, w: 6, h: 3 } },
      { kind: "text", id: "half", body: "h", width: "half" },
      { kind: "text", id: "off", body: "o", width: "half", layout: { x: 9, y: 0, w: 6, h: 3 } },
      { kind: "stats", id: "figures", items: [{ label: "A", value: "1" }] },
      { kind: "text", id: "float", body: "f", layout: { x: 0.5, y: 0, w: 6, h: 3 } },
    ],
  });
  assert.deepEqual(
    widgets.map((w) => [w.id, w.layout]),
    [
      ["placed", { x: 6, y: 0, w: 6, h: 3 }],
      ["half", { x: 0, y: 3, w: 6, h: 2 }],
      ["off", { x: 6, y: 3, w: 6, h: 2 }],
      ["figures", { x: 0, y: 5, w: 12, h: 2 }],
      ["float", { x: 0, y: 7, w: 12, h: 2 }],
    ],
  );
});

test("a score's unit that only restates its max is dropped", () => {
  const score = (extra: Record<string, unknown>) =>
    normalizeDashboard({ title: "T", widgets: [{ kind: "score", id: "s", label: "Score", value: 58, ...extra }] })
      .widgets[0] as { unit?: string; max?: number };
  assert.equal(score({ max: 100, unit: "/100" }).unit, undefined);
  assert.equal(score({ max: 100, unit: " / 100 " }).unit, undefined);
  assert.equal(score({ max: 100, unit: "pts" }).unit, "pts");
  assert.equal(score({ unit: "/100" }).unit, "/100");
  assert.equal(score({ max: 10, unit: "/100" }).unit, "/100");
});

test("a platform widget is read with its source, its view and its platform type, or drawn as a placeholder", () => {
  const read = (widget: Record<string, unknown>) =>
    normalizeDashboard({ title: "T", widgets: [{ id: "w", ...widget }] }).widgets[0] as unknown as Record<string, unknown>;
  const live = read({
    kind: "platform",
    source: "openaev",
    integration_id: "11111111-1111-4111-8111-111111111111",
    view: "donut",
    native: { widget_type: "donut", widget_config: {} },
  });
  assert.deepEqual(
    [live.kind, live.source, live.integrationId, live.view, live.size, live.nativeType],
    ["platform", "openaev", "11111111-1111-4111-8111-111111111111", "donut", "m", "donut"],
  );
  // No view: a type this build does not draw, shown as a link to the platform.
  assert.equal(read({ kind: "platform", source: "opencti", integration_id: "i", native: { type: "map" } }).view, undefined);
  assert.equal(read({ kind: "platform", source: "opencrq", integration_id: "i", view: "pie", native: { metric: "kev_findings" } }).view, undefined);
  for (const broken of [
    { kind: "platform", source: "splunk", integration_id: "i", native: {} },
    { kind: "platform", source: "opencti", native: { type: "donut" } },
    { kind: "platform", source: "opencti", integration_id: "i", native: "donut" },
  ]) {
    assert.equal(read(broken).kind, "unsupported");
  }
});
