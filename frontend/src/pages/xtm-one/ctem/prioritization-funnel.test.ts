/**
 * The geometry and links of the CTEM prioritization funnel (#11546):
 * widths shrink with the count and never grow, a layer without a count keeps
 * the width above it (it removes nothing), and each layer leads where its
 * count can be acted on. `yarn test:unit` (node:test).
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import type { CtemFunnel } from "../../../types/index.ts";
import {
  FUNNEL_CASES_PATH,
  FUNNEL_LIVE_FILL_ALPHA,
  FUNNEL_TEXT_SAFE_FILL_ALPHA,
  FUNNEL_MIN_WIDTH_PCT,
  FUNNEL_LAYER_COPY,
  FUNNEL_LAYER_FIELD,
  isOrganizationSettingsHref,
  FLOW_HEIGHT,
  FLOW_MIN_BRANCH,
  SAMPLE_FUNNEL,
  flowBandPath,
  flowFan,
  flowLayout,
  funnelDisplay,
  funnelIsMonotonic,
  funnelLayerHref,
  funnelLiveFillAlpha,
  funnelRows,
  funnelIsEmpty,
  funnelWidthPct,
  withCasesLayer,
} from "./prioritization-funnel.ts";

const casesLayer = (funnel: CtemFunnel) => funnel.layers.find((layer) => layer.key === "open_issues")!;

// #11662: a connected tenant never sees the sample's counts as its own.
test("the sample is drawn only when OpenCTI is not connected", () => {
  assert.equal(funnelDisplay({ funnel: SAMPLE_FUNNEL, openctiPresent: true, shape: 2 }), "live");
  assert.equal(funnelDisplay({ funnel: null, openctiPresent: false, shape: 2 }), "sample");
  assert.equal(funnelDisplay({ funnel: undefined, openctiPresent: false, shape: undefined }), "sample");
  // Cached before the shape stamp: the server rebuilds it at once.
  assert.equal(funnelDisplay({ funnel: undefined, openctiPresent: true, shape: undefined }), "pending");
  // Current shape and still no funnel: OpenCTI sent no count.
  assert.equal(funnelDisplay({ funnel: null, openctiPresent: true, shape: 2 }), "unavailable");
});

test("the first layer is drawn full width and the narrowest stays readable", () => {
  assert.equal(funnelWidthPct(4214, 4214), 100);
  assert.equal(funnelWidthPct(0, 4214), FUNNEL_MIN_WIDTH_PCT);
  assert.ok(funnelWidthPct(21, 4214) > FUNNEL_MIN_WIDTH_PCT);
  assert.equal(funnelWidthPct(5, 0), 0);
});

test("OpenCTI answering 0 vulnerabilities is the empty state, never a full bar of 0 (UX r1 B1 on #11557)", () => {
  const empty = {
    ...SAMPLE_FUNNEL,
    layers: SAMPLE_FUNNEL.layers.map((layer, i) => (i === 0 ? { ...layer, count: 0 } : layer)),
  };
  assert.equal(funnelIsEmpty(empty), true);
  assert.equal(funnelIsEmpty(SAMPLE_FUNNEL), false);
  assert.equal(funnelRows(empty)[0].widthPct, 0);
});

test("widths never grow down the funnel, and a layer without a count keeps the width above it", () => {
  const rows = funnelRows(SAMPLE_FUNNEL);
  const widths = rows.map((row) => row.widthPct);
  assert.deepEqual(widths, [...widths].sort((a, b) => b - a));
  const exploitable = rows.find((row) => row.layer.key === "exploitable")!;
  for (const key of ["exposed", "business_impact", "policy", "open_issues"]) {
    assert.equal(rows.find((row) => row.layer.key === key)!.widthPct, exploitable.widthPct, key);
  }
});

test("a drop names the live layer it was taken from, skipping a not configured one", () => {
  const funnel: CtemFunnel = {
    ...SAMPLE_FUNNEL,
    layers: [
      SAMPLE_FUNNEL.layers[0],
      { key: "relevant", count: null, drop: null, state: "not_configured", source: "organization", category: "software" },
      { key: "exploitable", count: 300, drop: 3914, state: "live", source: "opencti", category: null, basis: "known" },
    ],
  };
  const rows = funnelRows(funnel);
  assert.equal(rows[0].fromKey, null);
  assert.equal(rows[1].fromKey, null);
  assert.equal(rows[2].fromKey, "known");
});

test("the sample funnel is monotonic and its drops add up", () => {
  assert.equal(funnelIsMonotonic(SAMPLE_FUNNEL), true);
  const drops = SAMPLE_FUNNEL.layers.reduce((sum, layer) => sum + (layer.drop ?? 0), 0);
  assert.equal(drops, (SAMPLE_FUNNEL.layers[0].count as number) - SAMPLE_FUNNEL.needs_action);
});

test("a not configured layer leads to the Settings card of its field, never the bare first tab", () => {
  const notConfigured = (key: CtemFunnel["layers"][number]["key"]) =>
    funnelLayerHref({ key, count: null, drop: null, state: "not_configured", source: "organization", category: null }, "https://cti.example");
  assert.equal(notConfigured("relevant"), "/ctem/inventory/software?card=software");
  assert.equal(notConfigured("exposed"), "/ctem/inventory?card=assets_exposure");
  assert.equal(notConfigured("business_impact"), "/ctem/inventory/business?card=crown_jewels");
  assert.equal(notConfigured("policy"), "/ctem/inventory/exposure?card=prioritization_policy");
  assert.equal(notConfigured("open_issues"), "/ctem/inventory/software?card=software");
  // Every layer with a call to action names a field, so no CTA falls back to the first tab.
  for (const [key, copy] of Object.entries(FUNNEL_LAYER_COPY)) {
    if (copy.cta) assert.ok(FUNNEL_LAYER_FIELD[key as keyof typeof FUNNEL_LAYER_FIELD], key);
    if (copy.cta) assert.notEqual(notConfigured(key as keyof typeof FUNNEL_LAYER_COPY), "/settings/organization", key);
  }
  assert.equal(isOrganizationSettingsHref("/settings/organization/profile/business?card=crown_jewels"), true);
  assert.equal(isOrganizationSettingsHref("/settings/organization"), true);
  assert.equal(isOrganizationSettingsHref("/ctem/cases"), false);
});

test("a not configured layer leads to Settings, a live one to the OpenCTI list it counts", () => {
  const [known, , exploitable, exposed, , , openIssues] = SAMPLE_FUNNEL.layers;
  assert.equal(funnelLayerHref(exposed, "https://cti.example"), "/ctem/inventory?card=assets_exposure");
  assert.equal(funnelLayerHref(known, "https://cti.example/"), "https://cti.example/dashboard/arsenal/vulnerabilities");
  const filters = { mode: "or", filters: [{ key: ["x_opencti_cisa_kev"], values: ["true"] }], filterGroups: [] };
  const href = funnelLayerHref({ ...exploitable, filters }, "https://cti.example");
  // #11954: the key reaches OpenCTI as the string its URL keeps.
  const linked = { mode: "or", filters: [{ key: "x_opencti_cisa_kev", values: ["true"], operator: "eq", mode: "or" }], filterGroups: [] };
  assert.equal(href, `https://cti.example/dashboard/arsenal/vulnerabilities?filters=${encodeURIComponent(JSON.stringify(linked))}`);
  assert.equal(funnelLayerHref(known, null), null);
  assert.equal(funnelLayerHref(openIssues, "https://cti.example"), null);
});

test("a narrower live layer without a filter is not a link to the whole list (#11954)", () => {
  const [, relevant, exploitable] = SAMPLE_FUNNEL.layers;
  assert.equal(funnelLayerHref({ ...relevant, filters: null }, "https://cti.example"), null);
  assert.equal(funnelLayerHref({ ...exploitable, filters: undefined }, "https://cti.example"), null);
  const multiKey = { mode: "and", filters: [{ key: ["name", "aliases"], values: ["x"] }], filterGroups: [] };
  assert.equal(funnelLayerHref({ ...relevant, filters: multiKey }, "https://cti.example"), null);
});

// #11545 S5b: the last layer shows the open cases the Cases API counts.
test("the cases layer is the exact count of open cases and leads to the Cases list", () => {
  const funnel = withCasesLayer(SAMPLE_FUNNEL, { open: 7, total: 12 });
  const layer = casesLayer(funnel);
  assert.equal(layer.state, "live");
  assert.equal(layer.count, 7);
  assert.equal(layer.drop, null);
  assert.equal(funnelLayerHref(layer, "https://cti.example"), FUNNEL_CASES_PATH);
  assert.equal(funnelLayerHref(layer, null), FUNNEL_CASES_PATH);
  // Every other layer is left as the backend sent it.
  assert.deepEqual(funnel.layers.slice(0, -1), SAMPLE_FUNNEL.layers.slice(0, -1));
});

test("zero open cases with a software is a live 0, the exact count", () => {
  const layer = casesLayer(withCasesLayer(SAMPLE_FUNNEL, { open: 0, total: 0 }));
  assert.equal(layer.state, "live");
  assert.equal(layer.count, 0);
});

test("without a software, OpenAEV or any case, the cases layer is not configured, never an invented 0", () => {
  const bare: CtemFunnel = {
    ...SAMPLE_FUNNEL,
    layers: SAMPLE_FUNNEL.layers.map((layer) => {
      if (layer.key === "known") return { ...layer, asset_findings: null };
      if (layer.key === "relevant") return { ...layer, count: null, drop: null, state: "not_configured", category: "software" };
      return layer;
    }),
  };
  const layer = casesLayer(withCasesLayer(bare, { open: 0, total: 0 }));
  assert.equal(layer.state, "not_configured");
  assert.equal(layer.count, null);
  assert.equal(funnelLayerHref(layer, "https://cti.example"), "/ctem/inventory/software?card=software");
  // A case that exists (built before the stack was removed) is still counted.
  assert.equal(casesLayer(withCasesLayer(bare, { open: 2, total: 3 })).count, 2);
  // OpenAEV alone builds cases.
  const withAev: CtemFunnel = { ...bare, layers: bare.layers.map((l) => (l.key === "known" ? { ...l, asset_findings: 4 } : l)) };
  assert.equal(casesLayer(withCasesLayer(withAev, { open: 0, total: 0 })).state, "live");
});

test("before the Cases API answers, the funnel is unchanged", () => {
  assert.equal(withCasesLayer(SAMPLE_FUNNEL, undefined), SAMPLE_FUNNEL);
});

test("the cases layer counts another unit: it keeps the width above it, names no drop and leaves monotonicity alone", () => {
  const funnel = withCasesLayer(SAMPLE_FUNNEL, { open: 400, total: 400 });
  const rows = funnelRows(funnel);
  const exploitable = rows.find((row) => row.layer.key === "exploitable")!;
  const cases = rows.find((row) => row.layer.key === "open_issues")!;
  assert.equal(cases.widthPct, exploitable.widthPct);
  assert.equal(cases.fromKey, null);
  assert.equal(funnelIsMonotonic(funnel), true);
});

// dev:ctem-funnel-exclusion-flow, design round 1 B1-B4 (dev-docs/ctem-stage-viz.md 3.3).
const LIVE_FUNNEL: CtemFunnel = {
  ...SAMPLE_FUNNEL,
  layers: [
    { key: "known", count: 4200, drop: null, state: "live", source: "opencti", category: null },
    { key: "relevant", count: 1300, drop: 2900, state: "live", source: "organization", category: null },
    { key: "exploitable", count: 214, drop: 1086, state: "live", source: "opencti", category: null },
    { key: "exposed", count: 96, drop: 118, state: "live", source: "organization", category: null },
    { key: "business_impact", count: 41, drop: 55, state: "live", source: "organization", category: null },
    { key: "policy", count: 23, drop: 18, state: "live", source: "organization", category: null },
    { key: "open_issues", count: 7, drop: null, state: "live", source: "ctem_cases", category: null },
  ],
  needs_action: 23,
};

for (const width of [1080, 1728]) {
  test(`at ${width}px every branch is drawn at its true share, within 2 points (B1)`, () => {
    for (const funnel of [LIVE_FUNNEL, SAMPLE_FUNNEL]) {
      for (const step of flowLayout(funnel, width).steps) {
        if (step.trueShare === null) continue;
        const thin = step.removed! > 0 && step.branch === FLOW_MIN_BRANCH;
        if (!thin) assert.ok(Math.abs((step.drawnShare as number) - step.trueShare) <= 2, `${step.layer.key} ${step.drawnShare} vs ${step.trueShare}`);
        assert.ok(step.height <= step.entered + 1e-9, `${step.layer.key} grows`);
        assert.ok(step.height >= step.kept - 1e-9);
      }
    }
  });

  test(`at ${width}px no branch label box meets the cases outcomes box, policy not configured (#897 UX round 3, B4)`, () => {
    const policyOff: CtemFunnel = {
      ...LIVE_FUNNEL,
      layers: LIVE_FUNNEL.layers.map((l) => (l.key === "policy" ? { ...l, count: null, drop: null, state: "not_configured" } : l)),
    };
    for (const funnel of [LIVE_FUNNEL, policyOff, SAMPLE_FUNNEL]) {
      const layout = flowLayout(funnel, width);
      const box = { x0: layout.tiles.x, x1: layout.tiles.x + layout.tiles.width, y0: layout.outcomesTop, y1: layout.outcomesTop + layout.outcomesHeight };
      assert.ok(box.y1 <= FLOW_HEIGHT, `outcomes end inside the reserved height (${box.y1})`);
      assert.ok(box.y0 >= layout.tiles.ys[1] + layout.tiles.height, "outcomes sit below the tiles");
      for (const step of layout.steps) {
        if (step.labelY === null) continue;
        const label = { x0: step.labelX, x1: step.labelX + step.labelWidth, y0: step.labelY - 10, y1: step.labelY + 10 };
        const meets = label.x0 < box.x1 && label.x1 > box.x0 && label.y0 < box.y1 && label.y1 > box.y0;
        assert.ok(!meets, `${step.layer.key} label ${JSON.stringify(label)} meets outcomes ${JSON.stringify(box)}`);
      }
    }
  });

  test(`at ${width}px the case tiles end inside the measured width (B4)`, () => {
    const layout = flowLayout(LIVE_FUNNEL, width);
    assert.ok(layout.tiles.width > 0);
    assert.ok(layout.tiles.x + layout.tiles.width <= width);
    assert.ok(layout.node.x + layout.node.width < layout.tiles.x);
  });
}

test("the branch counts plus the last live layer equal Known, and counts never increase", () => {
  for (const funnel of [LIVE_FUNNEL, SAMPLE_FUNNEL]) {
    const steps = flowLayout(funnel, 1080).steps;
    const removed = steps.reduce((sum, step) => sum + (step.removed ?? 0), 0);
    const last = [...steps].reverse().find((step) => step.layer.state === "live" && step.layer.count !== null)!;
    assert.equal(removed + (last.layer.count as number), steps[0].layer.count);
    assert.equal(funnelIsMonotonic(funnel), true);
  }
});

test("a not configured exclusion draws a branch of zero thickness and keeps the band's height", () => {
  const steps = flowLayout(SAMPLE_FUNNEL, 1080).steps;
  const exploitable = steps.find((step) => step.layer.key === "exploitable")!;
  for (const key of ["exposed", "business_impact", "policy"]) {
    const step = steps.find((s) => s.layer.key === key)!;
    assert.equal(step.branch, 0, key);
    assert.equal(step.height, exploitable.height, key);
  }
});

test("the biggest removal is named, and branch labels form a staircase inside the flow (B3)", () => {
  const layout = flowLayout(LIVE_FUNNEL, 1080);
  assert.equal(layout.biggest, "relevant");
  const ys = layout.steps.map((step) => step.labelY).filter((y): y is number => y !== null);
  assert.deepEqual(ys, [...ys].sort((a, b) => b - a));
  assert.ok(ys.every((y) => y > 0 && y < FLOW_HEIGHT));
});

test("the case fan adds up to the open cases node (B2)", () => {
  const layout = flowLayout(LIVE_FUNNEL, 1080);
  const fan = flowFan(layout.node, 3, 4);
  const total = fan.reduce((sum, [a, b]) => sum + (b - a), 0);
  assert.ok(Math.abs(total - (layout.node.height - 24)) < 1e-9);
  assert.ok(Math.abs((fan[0][1] - fan[0][0]) / total - 3 / 7) < 1e-9);
  assert.deepEqual(flowFan(layout.node, 0, 0), []);
});

test("zero Known draws no band (the card shows its empty state)", () => {
  const empty = { ...SAMPLE_FUNNEL, layers: SAMPLE_FUNNEL.layers.map((l, i) => (i === 0 ? { ...l, count: 0 } : l)) };
  assert.equal(flowBandPath(flowLayout(empty, 1080)), null);
  assert.ok(flowBandPath(flowLayout(SAMPLE_FUNNEL, 1080))?.startsWith("M0,"));
});

// UX r1 B1 on #11658: the cases count sat on the full accent (2.84:1 dark, 2.41:1 light).
test("the cases count stays on a text-safe fill, at any depth; other layers keep their fill", () => {
  const funnel = withCasesLayer(SAMPLE_FUNNEL, { open: 7, total: 9 });
  const cases = casesLayer(funnel);
  for (let index = 0; index < FUNNEL_LIVE_FILL_ALPHA.length + 2; index += 1) {
    assert.ok(funnelLiveFillAlpha(cases, index) <= FUNNEL_TEXT_SAFE_FILL_ALPHA);
  }
  assert.equal(funnelLiveFillAlpha(cases, funnel.layers.indexOf(cases)), FUNNEL_TEXT_SAFE_FILL_ALPHA);
  const known = funnel.layers[0];
  FUNNEL_LIVE_FILL_ALPHA.forEach((alpha, index) => assert.equal(funnelLiveFillAlpha(known, index), alpha));
});
