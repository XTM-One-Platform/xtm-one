// SPDX-License-Identifier: Apache-2.0

/**
 * The geometry and copy keys of the CTEM prioritization funnel (#11546), kept
 * pure so `prioritization-funnel.test.ts` pins them without a DOM.
 *
 * The backend (`services/ctem_funnel.py`) sends the layers in order, each with
 * an exact count, the drop against the live layer above, and a state. This
 * module decides how wide each layer is drawn and where a layer leads:
 *
 * - widths follow the square root of the share of the first layer, floored so
 *   the narrowest live layer stays readable (4,214 to 21 is a 200x drop: a
 *   linear width would draw a hairline). The exact number is always printed;
 *   the width only orders the eye.
 * - a layer without a count (not configured, not available) is drawn at the
 *   width of the live layer above it, dashed: it removes nothing, so the
 *   funnel never looks narrower than what was measured.
 * - a not configured layer leads to the Settings > Organization card of the
 *   field it needs (its tab, scrolled to the card); a live one to the
 *   list it counts in OpenCTI, filtered when the backend sent the filter.
 */
import type { CtemFunnel, CtemFunnelLayer, CtemFunnelLayerKey } from "@/types";

import { profileFieldHref } from "../../../lib/organization-profile-tabs.ts";

import { OPENCTI_VULNERABILITIES_PATH, openCtiListHref } from "./opencti-link.ts";

/** Narrowest a live layer is drawn, in percent of the first layer's width. */
export const FUNNEL_MIN_WIDTH_PCT = 14;

/** The profile field each layer a not configured state asks for: its call to action opens the card that edits it. */
export const FUNNEL_LAYER_FIELD: Partial<Record<CtemFunnelLayerKey, string>> = {
  relevant: "tech_stack",
  exposed: "assets_exposure",
  business_impact: "crown_jewels",
  policy: "prioritization_policy",
  open_issues: "tech_stack",
};

/** Settings > Organization, on the tab and card of the field a not configured layer needs (#12329 tabs). */
export function funnelSettingsHref(key: CtemFunnelLayerKey): string {
  return profileFieldHref(FUNNEL_LAYER_FIELD[key] ?? "");
}

/** Whether a link opens Settings > Organization (the profile page, any tab). */
export function isOrganizationSettingsHref(href: string): boolean {
  return href === "/settings/organization" || href.startsWith("/settings/organization?") || href.startsWith("/settings/organization/profile/");
}

/** Whether a link opens CTEM > Inventory, where the technology and exposure cards live (#1178). */
export function isInventoryHref(href: string): boolean {
  return href === "/ctem/inventory" || href.startsWith("/ctem/inventory?") || href.startsWith("/ctem/inventory/");
}

/** Name, definition and call to action of each layer (English catalog keys). */
export const FUNNEL_LAYER_COPY: Record<CtemFunnelLayerKey, { name: string; definition: string; cta?: string }> = {
  known: {
    name: "Known vulnerabilities",
    definition: "Every vulnerability your OpenCTI access can read.",
  },
  relevant: {
    name: "Relevant to you",
    definition: "Vulnerabilities of your software, matched on CPE, or on product and vendor.",
    cta: "Add your software",
  },
  exploitable: {
    name: "Exploitable",
    definition: "Known exploited (CISA KEV) or with an EPSS above {threshold}.",
  },
  exposed: {
    name: "Exposed",
    definition: "On a reachable asset: internet-facing, or a user endpoint (phishing, browser, email).",
    cta: "Add your assets and exposure",
  },
  business_impact: {
    name: "Business impact",
    definition: "On an asset that supports a crown jewel.",
    cta: "Map crown jewels to assets",
  },
  policy: {
    name: "After policy",
    definition: "Left after your prioritization policy: thresholds, exclusions and accepted risk.",
    cta: "Set a prioritization policy",
  },
  // The last layer counts cases, not vulnerabilities (#11545 S5b): the open
  // issues above, grouped by the fix they share. Not configured without a tech
  // stack or OpenAEV, the two inputs cases are built from.
  open_issues: {
    name: "Cases",
    definition: "Open cases: the issues above grouped by the fix they share, needing attention or in progress.",
    cta: "Add your software",
  },
};

/** The CTEM Cases list, where the cases layer leads. */
export const FUNNEL_CASES_PATH = "/ctem/cases";

/** What the Cases API answered for the caller (`GET /ctem-cases`, #11613). */
export interface FunnelCases {
  /** Open cases (new to validating, #1503), among those the caller sees. */
  open: number;
  /** Every case the caller sees, whatever its state. */
  total: number;
}

/** The case states of the Cases board (S5c), their counts as the Cases API sent them. */
export interface FunnelCaseStates {
  /** Open cases a person must decide on (#1503). */
  waiting_decision: number;
  /** Open cases the agents work: new, analyzing, remediating, validating (#1503). */
  in_work: number;
  /** Mitigated, watching (#857): the re-test stays on until the permanent fix. */
  mitigated_watching?: number;
  resolved: number;
  accepted_risk: number;
}

/** Accent opacity (percent) of a live layer's fill, deeper as the funnel narrows. */
export const FUNNEL_LIVE_FILL_ALPHA = [25, 40, 60, 75, 85, 100, 100] as const;

/** The deepest accent fill the count text holds 4.5:1 on in both themes
 *  (UX r1 B1 on #11658: full accent gave 2.84:1 dark, 2.41:1 light; /60 gives 5.67 and 5.48). */
export const FUNNEL_TEXT_SAFE_FILL_ALPHA = 60;

/** Accent opacity of the live layer drawn at `index`: the cases layer, added
 *  last (#11545 S5b), never goes past the text-safe fill. */
export function funnelLiveFillAlpha(layer: CtemFunnelLayer, index: number): number {
  const alpha = FUNNEL_LIVE_FILL_ALPHA[Math.min(Math.max(index, 0), FUNNEL_LIVE_FILL_ALPHA.length - 1)];
  return isCasesLayer(layer) ? Math.min(alpha, FUNNEL_TEXT_SAFE_FILL_ALPHA) : alpha;
}

/** Whether a layer counts cases rather than vulnerabilities. */
export function isCasesLayer(layer: CtemFunnelLayer): boolean {
  return layer.source === "ctem_cases";
}

/**
 * The funnel with its last layer filled from the Cases API (#11545 S5b).
 *
 * - `cases` undefined (not read yet, or the read failed): the layer stays as
 *   the backend sent it, without a count.
 * - cases cannot be built (no software inventory and no OpenAEV) and none exists: not
 *   configured, leading to Settings > Organization; never an invented 0.
 * - otherwise live, with the exact count of open cases and no drop: a case
 *   groups the issues above, it removes none of them.
 */
export function withCasesLayer(funnel: CtemFunnel, cases: FunnelCases | undefined): CtemFunnel {
  if (!cases) return funnel;
  const relevant = funnel.layers.find((layer) => layer.key === "relevant");
  const known = funnel.layers[0];
  const buildable = relevant?.state === "live" || known?.asset_findings != null || cases.total > 0;
  return {
    ...funnel,
    layers: funnel.layers.map((layer) => {
      if (!isCasesLayer(layer)) return layer;
      return buildable
        ? { ...layer, count: cases.open, drop: null, state: "live", category: null }
        : { ...layer, count: null, drop: null, state: "not_configured", category: "tech_stack" };
    }),
  };
}

export interface FunnelRow {
  layer: CtemFunnelLayer;
  /** Drawn width, in percent of the first layer's. */
  widthPct: number;
  /** The previous live layer's name key, for the drop line; null on the first. */
  fromKey: CtemFunnelLayerKey | null;
}

/** Width of a live layer of `count` against the first layer's `total`. A first
 *  layer of 0 draws every layer at zero width (UX r1 B1 on #11557: a full bar
 *  of 0 read as a measured volume); the card shows its empty state then. */
export function funnelWidthPct(count: number, total: number): number {
  if (total <= 0) return 0;
  const share = Math.min(1, Math.max(0, count / total));
  return Math.round((FUNNEL_MIN_WIDTH_PCT + (100 - FUNNEL_MIN_WIDTH_PCT) * Math.sqrt(share)) * 10) / 10;
}

/** The rows to draw, in order: a layer without a count keeps the width above it. */
export function funnelRows(funnel: CtemFunnel): FunnelRow[] {
  const total = funnel.layers[0]?.count ?? 0;
  let width = 100;
  let fromKey: CtemFunnelLayerKey | null = null;
  return funnel.layers.map((layer) => {
    // Cases are another unit: the cases layer keeps the width above it and
    // names no drop (it groups the issues, it does not filter them).
    const live = layer.state === "live" && layer.count !== null && !isCasesLayer(layer);
    if (live) width = funnelWidthPct(layer.count as number, total);
    const row: FunnelRow = { layer, widthPct: width, fromKey: live ? fromKey : null };
    if (live) fromKey = layer.key;
    return row;
  });
}

/** Whether OpenCTI answered and holds no vulnerability: the card draws its
 *  empty state instead of a funnel of zeros (UX r1 B1 on #11557). */
export function funnelIsEmpty(funnel: CtemFunnel): boolean {
  const known = funnel.layers[0];
  return known?.state === "live" && known.count === 0;
}

/** Where a layer leads: Settings for a not configured one, the Cases list for
 *  the live cases layer, the OpenCTI list for a live OpenCTI one (#11954):
 *  the known layer opens the whole list, every narrower layer the list its
 *  filter keeps, and a narrower layer without a filter OpenCTI can read is
 *  not a link (it would open a wider set than its figure). */
export function funnelLayerHref(layer: CtemFunnelLayer, openctiUrl: string | null | undefined): string | null {
  if (layer.state === "not_configured") return funnelSettingsHref(layer.key);
  if (layer.state === "live" && isCasesLayer(layer)) return FUNNEL_CASES_PATH;
  if (layer.state !== "live") return null;
  if (layer.source !== "opencti" && layer.key !== "relevant") return null;
  if (layer.key === "known") return openCtiListHref(openctiUrl, OPENCTI_VULNERABILITIES_PATH);
  if (!layer.filters) return null;
  return openCtiListHref(openctiUrl, OPENCTI_VULNERABILITIES_PATH, layer.filters);
}

/** The live vulnerability layers' counts never increase down the funnel (the
 *  backend caps them; the UI checks). The cases layer counts another unit. */
export function funnelIsMonotonic(funnel: CtemFunnel): boolean {
  const counts = funnel.layers
    .filter((l) => l.state === "live" && l.count !== null && !isCasesLayer(l))
    .map((l) => l.count as number);
  return counts.every((count, i) => i === 0 || count <= counts[i - 1]);
}

// ---------------------------------------------------------------------------
// The exclusion flow (dev-docs/ctem-stage-viz.md 3.3, dev:ctem-funnel-exclusion-flow):
// the stage drawing since the design round of 13 Nov 2026. One band, left to
// right, narrowing at each live layer; what a layer removed leaves as a branch
// labelled with its count and reason; the band ends in the open cases node,
// which fans out into the Needs attention and In progress tiles.
// Pixel geometry lives here, pure, so the tests pin it without a DOM.
// ---------------------------------------------------------------------------

/** Fixed height of the flow, reserved before data arrives (no layout shift). */
export const FLOW_HEIGHT = 360;
/** Top of the band: room above it for each step's figure and two-line name. */
export const FLOW_TOP = 78;
/** Height of the band where it starts (Known vulnerabilities). */
const FLOW_BAND_HEIGHT = 150;
/** Vertical step of the branch label staircase (design round 1, B3). */
const FLOW_LABEL_ROW = 28;
/** Thinnest branch drawn when a layer removed something (B1). */
export const FLOW_MIN_BRANCH = 2;
/** Longest ease of the kept band to its own height, in px (B1). */
const FLOW_EASE_MAX = 48;
/** Below this figure width the cases column is the narrow one (1080 layout). */
const FLOW_WIDE_FROM = 1000;

/** The reason a layer removed what it removed, in plain words (catalog keys). */
export const FLOW_REASON: Partial<Record<CtemFunnelLayerKey, string>> = {
  relevant: "not in your software",
  exploitable: "not exploitable",
  exposed: "not exposed",
  business_impact: "low business impact",
  policy: "deprioritized by policy",
};

export interface FlowStep {
  layer: CtemFunnelLayer;
  /** Left edge of the step, in px from the figure's left. */
  x: number;
  /** Band height entering the step. */
  entered: number;
  /** Band height kept at the split, before it eases (entered - branch). */
  kept: number;
  /** Band height after the ease: never taller than what entered, never shorter than kept. */
  height: number;
  /** Branch root thickness: 0 on the first step and on a step without a count. */
  branch: number;
  /** What the layer removed (its `drop`), null when it removed nothing measurable. */
  removed: number | null;
  /** True share removed (drop / entered count), and the drawn share (branch / entered height), in percent. */
  trueShare: number | null;
  drawnShare: number | null;
  /** Baseline of the branch label (staircase), null on the first step. */
  labelY: number | null;
  /** Left edge of the branch label, past the branch curve. */
  labelX: number;
  /** Width of the branch label box: to the figure's edge, or to `labelRight` on a row beside or below the outcomes block's top. */
  labelWidth: number;
  /** The previous live layer's key, for the drop text; null on the first. */
  fromKey: CtemFunnelLayerKey | null;
}

export interface FlowLayout {
  width: number;
  /** Width of the band, the cases column on its right. */
  flowWidth: number;
  pitch: number;
  steps: FlowStep[];
  /** Key of the step that removed the most, its label drawn first to the eye. */
  biggest: CtemFunnelLayerKey | null;
  node: { x: number; y: number; width: number; height: number };
  tiles: { x: number; width: number; height: number; ys: [number, number] };
  /** Right edge of a branch label on a row beside the outcomes block: 8 px before the cases column. */
  labelRight: number;
  /**
   * Top of the cases outcomes block (mitigated, resolved, accepted risk),
   * placed below the highest branch label row so the top label, which runs
   * into the cases column, never draws over it; the rows beside the block end
   * at `labelRight` (UX round 3 on #897, B4: "Set a prioritization policy"
   * drew over "0 resolved" at 1080).
   */
  outcomesTop: number;
  /** Height reserved for the outcomes block. */
  outcomesHeight: number;
}

/** Height reserved for the outcomes block: three caption lines and their gaps (1080 wraps the mitigated line). */
const FLOW_OUTCOMES_HEIGHT = 60;
/** Half height of a branch label box (h-5, centred on its baseline row). */
const FLOW_LABEL_HALF = 10;

/** The flow laid out at `width` px (the measured figure width). */
export function flowLayout(funnel: CtemFunnel, width: number): FlowLayout {
  const layers = funnel.layers.filter((layer) => !isCasesLayer(layer));
  const total = layers[0]?.state === "live" ? (layers[0].count ?? 0) : 0;
  const right = width < FLOW_WIDE_FROM ? 244 : 300;
  const flowWidth = Math.max(width - right, 0);
  const pitch = layers.length > 0 ? flowWidth / layers.length : 0;
  const start = total > 0 ? FLOW_BAND_HEIGHT : 0;
  const steps: FlowStep[] = [];
  let height = start;
  let prevCount = total;
  let fromKey: CtemFunnelLayerKey | null = null;
  let biggest: CtemFunnelLayerKey | null = null;
  let biggestDrop = 0;
  layers.forEach((layer, index) => {
    const x = index * pitch;
    const labelY = index === 0 ? null : FLOW_HEIGHT - 32 - (index - 1) * FLOW_LABEL_ROW;
    const labelX = x + Math.min(pitch * 0.5, 64) + 6;
    const labelWidth = Math.max(width - labelX - 4, 0);
    const live = layer.state === "live" && layer.count !== null;
    if (index === 0 || !live || prevCount <= 0) {
      steps.push({ layer, x, entered: height, kept: height, height, branch: 0, removed: null, trueShare: null, drawnShare: null, labelY, labelX, labelWidth, fromKey: null });
      if (index === 0 && live) fromKey = layer.key;
      return;
    }
    const count = layer.count as number;
    const removed = layer.drop ?? Math.max(prevCount - count, 0);
    const entered = height;
    let branch = (entered * removed) / prevCount;
    if (removed > 0) branch = Math.min(Math.max(branch, FLOW_MIN_BRANCH), entered);
    const kept = entered - branch;
    const target = FLOW_BAND_HEIGHT * Math.max(Math.sqrt(Math.min(count / total, 1)), FUNNEL_MIN_WIDTH_PCT / 100);
    height = Math.max(Math.min(target, entered), kept);
    steps.push({
      layer,
      x,
      entered,
      kept,
      height,
      branch,
      removed,
      trueShare: (removed / prevCount) * 100,
      drawnShare: entered > 0 ? (branch / entered) * 100 : 0,
      labelY,
      labelX,
      labelWidth,
      fromKey,
    });
    if (removed > biggestDrop) {
      biggestDrop = removed;
      biggest = layer.key;
    }
    prevCount = count;
    fromKey = layer.key;
  });
  const nodeWidth = width < FLOW_WIDE_FROM ? 88 : 104;
  const node = { x: flowWidth + 8, y: FLOW_TOP - 6, width: nodeWidth, height: 64 };
  const tileX = node.x + node.width + 28;
  const tiles = { x: tileX, width: Math.max(width - tileX - 2, 0), height: 48, ys: [FLOW_TOP - 24, FLOW_TOP + 32] as [number, number] };
  const labelRight = Math.max(tileX - 8, 0);
  const rows = steps.map((step) => step.labelY).filter((y): y is number => y !== null);
  const topRow = rows.length > 0 ? Math.min(...rows) : FLOW_HEIGHT;
  const underTiles = tiles.ys[1] + tiles.height + 12;
  const underLabels = Math.max(underTiles, topRow + FLOW_LABEL_HALF + 8);
  // Below the top label row when the block still fits the reserved height;
  // else under the tiles, the rows beside it then ending at labelRight.
  const outcomesTop = underLabels + FLOW_OUTCOMES_HEIGHT <= FLOW_HEIGHT ? underLabels : underTiles;
  for (const step of steps) {
    if (step.labelY === null) continue;
    // Every row from the block's top down ends before the column: a wrapped
    // translation that runs past the reserved height still clears them.
    const beside = step.labelY + FLOW_LABEL_HALF > outcomesTop;
    if (beside) step.labelWidth = Math.max(Math.min(step.labelWidth, labelRight - step.labelX), 0);
  }
  return { width, flowWidth, pitch, steps, biggest, node, tiles, labelRight, outcomesTop, outcomesHeight: FLOW_OUTCOMES_HEIGHT };
}

const f = (value: number) => value.toFixed(1);

/** The band: top edge straight, bottom edge stepping down at each split then easing to its own height. */
export function flowBandPath(layout: FlowLayout): string | null {
  const { steps, flowWidth, pitch } = layout;
  if (steps.length === 0 || (steps[0]?.entered ?? 0) <= 0) return null;
  const ease = Math.min(pitch * 0.5, FLOW_EASE_MAX);
  let d = `M0,${FLOW_TOP} L${f(flowWidth)},${FLOW_TOP} `;
  for (let i = steps.length - 1; i >= 0; i -= 1) {
    const step = steps[i];
    const x0 = step.x;
    const x1 = i + 1 < steps.length ? steps[i + 1].x : flowWidth;
    const kept = FLOW_TOP + step.kept;
    const end = FLOW_TOP + step.height;
    d += `L${f(x1)},${f(end)} L${f(x0 + ease)},${f(end)} C${f(x0 + ease / 2)},${f(end)} ${f(x0 + ease / 2)},${f(kept)} ${f(x0)},${f(kept)} `;
    if (i > 0) d += `L${f(x0)},${f(FLOW_TOP + steps[i - 1].height)} `;
  }
  return `${d}Z`;
}

/** A removed branch: its root on the band's bottom at the split, its end at its label row. */
export function flowBranchPath(layout: FlowLayout, step: FlowStep): string {
  const dx = Math.min(layout.pitch * 0.5, 64);
  const x = step.x;
  const y0 = FLOW_TOP + step.kept;
  const y1 = y0 + step.branch;
  const row = step.labelY ?? FLOW_HEIGHT;
  const end = Math.min(step.branch, 18);
  const e0 = row - end / 2;
  const e1 = e0 + end;
  return `M${f(x)},${f(y0)} C${f(x + dx * 0.9)},${f(y0)} ${f(x)},${f(e0)} ${f(x + dx)},${f(e0)} L${f(x + dx)},${f(e1)} C${f(x - 4)},${f(e1)} ${f(x)},${f(y1)} ${f(x)},${f(y1)} Z`;
}

/** A not configured exclusion: a dashed hairline that removes nothing. */
export function flowDashedPath(layout: FlowLayout, step: FlowStep): string {
  const dx = Math.min(layout.pitch * 0.5, 64);
  const row = step.labelY ?? FLOW_HEIGHT;
  const x = step.x;
  return `M${f(x)},${f(FLOW_TOP + step.height)} L${f(x)},${f(row - 10)} C${f(x)},${f(row)} ${f(x + 4)},${f(row)} ${f(x + dx)},${f(row)}`;
}

/** A fan link from the open cases node to its tile (`index` 0: Waiting for decision, 1: In work). */
export function flowFanPath(layout: FlowLayout, s0: number, s1: number, index: number): string {
  const { node, tiles } = layout;
  const centre = tiles.ys[index] + tiles.height / 2;
  const half = (s1 - s0) / 2;
  const t0 = centre - half;
  const t1 = centre + half;
  const xa = node.x + node.width;
  const xb = tiles.x;
  return `M${f(xa)},${f(s0)} C${f(xa + 14)},${f(s0)} ${f(xb - 14)},${f(t0)} ${f(xb)},${f(t0)} L${f(xb)},${f(t1)} C${f(xb - 14)},${f(t1)} ${f(xa + 14)},${f(s1)} ${f(xa)},${f(s1)} Z`;
}

/** The two fan links out of the open cases node, each as thick as its share (B2). */
export function flowFan(node: FlowLayout["node"], needsAttention: number, inProgress: number): [number, number][] {
  const open = needsAttention + inProgress;
  const top = node.y + 12;
  const span = node.height - 24;
  if (open <= 0) return [];
  const split = (span * needsAttention) / open;
  return [
    [top, top + split],
    [top + split, top + span],
  ];
}

/** What the funnel slot draws (#11662):
 *  - `live`: the posture carries a funnel;
 *  - `sample`: no OpenCTI connected, the sample badged Sample;
 *  - `pending`: OpenCTI connected but the posture was cached before the server
 *    stamped its shape (built before the funnel shipped). The server serves it
 *    once and rebuilds at once; the posture poll swaps the live funnel in;
 *  - `unavailable`: OpenCTI connected, current shape, and still no funnel
 *    (OpenCTI gave no vulnerability count). Never the sample: the sample's
 *    counts would read as the tenant's own. */
export type FunnelDisplay = "live" | "sample" | "pending" | "unavailable";

export function funnelDisplay(input: {
  funnel: CtemFunnel | null | undefined;
  openctiPresent: boolean;
  shape: number | null | undefined;
}): FunnelDisplay {
  if (input.funnel) return "live";
  if (!input.openctiPresent) return "sample";
  return input.shape === undefined || input.shape === null ? "pending" : "unavailable";
}

/** A sample funnel for a tenant with no OpenCTI connected (badged Sample). */
export const SAMPLE_FUNNEL: CtemFunnel = {
  layers: [
    { key: "known", count: 4214, drop: null, state: "live", source: "opencti", category: null, asset_findings: 89 },
    { key: "relevant", count: 512, drop: 3702, state: "live", source: "organization", category: null },
    { key: "exploitable", count: 21, drop: 491, state: "live", source: "opencti", category: null, basis: "relevant" },
    { key: "exposed", count: null, drop: null, state: "not_configured", source: "organization", category: "assets" },
    { key: "business_impact", count: null, drop: null, state: "not_configured", source: "organization", category: "crown_jewels" },
    { key: "policy", count: null, drop: null, state: "not_configured", source: "organization", category: "prioritization_policy" },
    { key: "open_issues", count: null, drop: null, state: "not_available", source: "ctem_cases", category: null },
  ],
  needs_action: 21,
  epss_threshold: 0.5,
  capped: false,
};
