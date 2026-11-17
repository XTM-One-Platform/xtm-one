/**
 * The validation views' arithmetic and geometry against the reference's
 * numbers (`dev-docs/dashboard-rendering-reference.md`, OpenAEV sections 9-14):
 * radii, angles, thresholds, colours, sizes - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import type { WidgetOutcome, WidgetOutcomesData } from "@/types";

import {
  FLOW,
  ORB,
  PATH,
  RING,
  SAMPLE_GATES,
  SAMPLE_OUTCOMES,
  SCORE_GAUGE,
  abbreviate,
  arcPath,
  averageColor,
  coverageAccent,
  domainCards,
  exposureBand,
  exposureScore,
  flowGates,
  gateRateColor,
  matrixModel,
  orbRing,
  orbitName,
  orbitPositions,
  particlesFor,
  pathEdges,
  pathNodes,
  pathRate,
  resilience,
  resilienceAccent,
  ringSegments,
  rowArc,
  scoreBreakdown,
  scoreGaugeArcs,
  truncateLabel,
} from "./outcomes.ts";

const outcome = (label: string, success: number, failed: number, pending = 0, extra: Partial<WidgetOutcome> = {}): WidgetOutcome => ({
  label,
  success,
  failed,
  pending,
  parts: [],
  ...extra,
});
const close = (actual: number, expected: number) => assert.ok(Math.abs(actual - expected) < 1e-6, `${actual} != ${expected}`);

test("scores: exposure is the failed share of resolved checks, resilience the succeeded one, pending ignored", () => {
  const counts = outcome("", 125, 77, 19);
  assert.equal(exposureScore(counts), 38);
  assert.equal(resilience(counts), 62);
  assert.equal(exposureScore(outcome("", 0, 0, 9)), 0);
  assert.equal(resilience(outcome("", 0, 0, 9)), 0);
});

test("exposure bands: under 25, 50, 75, then critical, with the theme's colours", () => {
  assert.deepEqual([0, 24, 25, 49, 50, 74, 75, 100].map((score) => exposureBand(score, "dark").band), ["low", "low", "moderate", "moderate", "high", "high", "critical", "critical"]);
  assert.deepEqual(
    ["low", "moderate", "high", "critical"].map((_, i) => exposureBand(i * 25, "dark").color),
    ["#17ab1f", "#ffa726", "var(--color-feedback-warning-primary)", "#f14337"],
  );
  assert.equal(exposureBand(30, "light").color, "#ed6c02");
});

test("score gauge: 270° from -225°, radius 82 in a 200 viewBox, the needle at the tip", () => {
  assert.deepEqual([SCORE_GAUGE.size, SCORE_GAUGE.radius, SCORE_GAUGE.start, SCORE_GAUGE.sweep, SCORE_GAUGE.stroke, SCORE_GAUGE.sweepSize], [200, 82, -225, 270, 12, 260]);
  const half = scoreGaugeArcs(0.5);
  // The track: from the bottom left, large arc, clockwise, to the bottom right.
  assert.equal(half.track, arcPath(100, 100, 82, -225, 45));
  assert.match(half.track, / A 82 82 0 1 1 /);
  // Half way: the top of the gauge.
  close(half.tip.x, 100);
  close(half.tip.y, 18);
  // At 0 the arc still has a dot's length.
  const none = scoreGaugeArcs(0);
  close(none.tip.x, 100 + 82 * Math.cos((-225 + 0.27) * (Math.PI / 180)));
  assert.match(none.value, / A 82 82 0 0 1 /);
});

test("score gauge breakdown: the first four labels, each its own exposure and band", () => {
  const items = [outcome("A", 3, 1), outcome("B", 1, 1), outcome("C", 0, 4), outcome("D", 1, 0), outcome("E", 0, 1)];
  assert.deepEqual(
    scoreBreakdown(items, "dark").map(({ item, exposure, color }) => [item.label, exposure, color]),
    [["A", 25, "#ffa726"], ["B", 50, "var(--color-feedback-warning-primary)"], ["C", 100, "#f14337"], ["D", 0, "#17ab1f"]],
  );
});

test("orb: ring 84 and orbit 102 in a 256 viewBox, the marker at the top, eight members at most", () => {
  assert.deepEqual([ORB.size, ORB.ring, ORB.orbit, ORB.node, ORB.maxOrbit, ORB.orbitSeconds], [256, 84, 102, 13, 8, 90]);
  const ring = orbRing(0.25);
  close(ring.circumference, 527.7875658030853);
  close(ring.offset, ring.circumference * 0.75);
  close(ring.marker.x, 128);
  close(ring.marker.y, 44);
  assert.equal(ring.rotation, 90);
  assert.equal(orbRing(2).offset, 0);
  const at = orbitPositions(4);
  close(at[0].x, 128);
  close(at[0].y, 26);
  close(at[1].x, 230);
  close(at[1].y, 128);
});

test("orbit members: a type's abbreviation, a long name cut", () => {
  assert.deepEqual(["EDR", "SIEM", "EMAIL_SECURITY", "VULNERABILITY_SCANNER", "LLM_FIREWALL", undefined].map(abbreviate), ["EDR", "SIEM", "ES", "VS", "LF", "?"]);
  assert.equal(orbitName("CrowdStrike Falcon Insight XDR"), "CrowdStrike Falcon In\u2026");
  assert.equal(orbitName("Exactly twenty-two ch."), "Exactly twenty-two ch.");
});

test("attack flow: 680x240, gates evenly between 158 and 528, only those with a check", () => {
  assert.deepEqual([FLOW.width, FLOW.height, FLOW.cy, FLOW.adversaryX, FLOW.assetsX, FLOW.captionY, FLOW.valueY, FLOW.gaugeY, FLOW.percentY], [680, 240, 104, 52, 624, 38, 178, 188, 208]);
  const items = [outcome("Vulnerability", 23, 17, 4, { color: "#0fbcff" }), outcome("Idle", 0, 0, 0), outcome("Prevention", 58, 22, 6), outcome("Waiting", 0, 0, 5)];
  const flow = flowGates(items, "dark");
  assert.deepEqual([flow.attempts, flow.pending, flow.breached], [135, 15, 39]);
  assert.deepEqual(flow.gates.map((gate) => gate.item.label), ["Vulnerability", "Prevention", "Waiting"]);
  const [vulnerability, prevention, waiting] = flow.gates;
  close(vulnerability.x, 158 + 370 * (0.5 / 3));
  close(vulnerability.column, 370 / 3);
  close(vulnerability.labelWidth, 370 / 3 - 10);
  assert.equal(vulnerability.gaugeWidth, 56);
  // A label's own colour, else the cycle by its place among the gates.
  assert.deepEqual([vulnerability.color, prevention.color, waiting.color], ["#0fbcff", "#818cf8", "#c084fc"]);
  // Rates and their colours; a gate with nothing resolved is muted, its fill and gauge empty.
  close(vulnerability.rate, 23 / 40);
  assert.equal(vulnerability.rateColor, "#ffa726");
  close(vulnerability.fill, (23 / 40) * 104);
  close(vulnerability.gaugeFill, 56 * (23 / 40));
  assert.deepEqual([waiting.rate, waiting.fill, waiting.gaugeFill, waiting.rateColor], [0, 0, 0, "#75829A"]);
  assert.deepEqual([vulnerability.stopped, prevention.stopped, waiting.stopped, flow.breachParticles], [1, 3, 0, 2]);
  // One gate fills the whole span.
  close(flowGates([outcome("A", 1, 0)], "dark").gates[0].x, 343);
  assert.equal(flowGates([outcome("A", 1, 0)], "dark").gates[0].gaugeWidth, 56);
  assert.equal(flowGates(Array.from({ length: 12 }, (_, i) => outcome(`G${i}`, 1, 0)), "dark").gates[0].gaugeWidth, 32);
});

test("attack flow details: rate colours, particles, labels cut to their column", () => {
  assert.deepEqual([0.75, 0.5, 0.25, 0.2].map((rate) => gateRateColor(rate, 1, "dark")), ["#17ab1f", "#ffa726", "#ff7043", "#f14337"]);
  assert.equal(gateRateColor(0, 0, "light"), "#6E7788");
  assert.deepEqual([particlesFor(0, 100), particlesFor(1, 100), particlesFor(20, 100), particlesFor(90, 100)], [0, 1, 2, 3]);
  assert.equal(truncateLabel("VULNERABILITY", 9.5, 60), "VULNERABI…");
  assert.equal(truncateLabel("Prevention", 9.5, 113), "Prevention");
});

test("ring gauge: radius 58, segments from the top with 3° gaps, accents by resilience", () => {
  assert.deepEqual([RING.size, RING.radius, RING.stroke, RING.gapDegrees], [132, 58, 5, 3]);
  const circumference = 2 * Math.PI * 58;
  const gap = (3 / 360) * circumference;
  const segments = ringSegments(outcome("", 64, 27, 9), "dark");
  assert.deepEqual(segments.map((s) => [s.status, s.color]), [["success", "#17ab1f"], ["failed", "#f14337"], ["pending", "#75829A"]]);
  close(segments[0].length, 0.64 * circumference - gap);
  close(segments[1].offset, -0.64 * circumference);
  assert.equal(segments[2].dasharray, `${segments[2].length} ${circumference - segments[2].length}`);
  assert.deepEqual(ringSegments(outcome("", 0, 3, 0), "dark").map((s) => s.status), ["failed"]);
  assert.deepEqual(ringSegments(outcome("", 0, 0, 0), "dark"), []);
  // A tiny share keeps half a pixel.
  assert.equal(ringSegments(outcome("", 1000, 1, 0), "dark")[1].length, 0.5);
  assert.deepEqual([75, 50, 25, 24].map((value) => resilienceAccent(value, "dark")), ["#17ab1f", "#ffa726", "#e6700f", "#f14337"]);
});

const MATRIX: WidgetOutcomesData = {
  chains: [
    { id: "mitre-atlas", label: "MITRE ATLAS", groups: [{ id: "recon", label: "Reconnaissance" }] },
    { id: "mitre-attack", label: "MITRE ATT&CK", default: true, groups: [{ id: "init", label: "Initial Access" }, { id: "exec", label: "Execution" }] },
  ],
  reference: [
    { id: "ap-valid", label: "Valid Accounts", code: "T1078", phases: ["init", "exec"] },
    { id: "ap-phish", label: "Phishing", code: "T1566", phases: ["init"], subIds: ["ap-phish-link"] },
    { id: "ap-cmd", label: "Command and Scripting Interpreter", code: "T1059", phases: ["exec"] },
    { id: "ap-ml", label: "Search for Victim's Publicly Available Research", code: "AML.T0000", phases: ["recon"] },
  ],
  items: [outcome("Phishing", 2, 1, 0, { id: "ap-phish" }), outcome("Valid Accounts", 0, 0, 0, { id: "ap-valid" })],
};

test("technique matrix: the default chain's groups in order, labels sorted by name, coverage of the chain", () => {
  const model = matrixModel(MATRIX, null, "all");
  assert.equal(model.chain, "mitre-attack");
  assert.deepEqual(model.columns.map((column) => [column.label, column.cells.map((cell) => cell.code)]), [
    ["Initial Access", ["T1566", "T1078"]],
    ["Execution", ["T1059", "T1078"]],
  ]);
  // A label under two groups counts once; one with an outcome is covered, even untested.
  assert.deepEqual([model.total, model.covered, model.gaps, model.percent], [3, 2, 1, 67]);
  const [phishing, valid] = model.columns[0].cells;
  assert.deepEqual([phishing.tested, phishing.successCount, phishing.total, phishing.subIds], [true, 2, 3, ["ap-phish-link"]]);
  close(phishing.rate ?? -1, 2 / 3);
  assert.deepEqual([valid.present, valid.tested, valid.rate], [true, false, null]);
  // Another chain picked; an unknown one falls back to the default.
  assert.deepEqual(matrixModel(MATRIX, "mitre-atlas", "all").columns.map((c) => c.label), ["Reconnaissance"]);
  assert.equal(matrixModel(MATRIX, "gone", "all").chain, "mitre-attack");
});

test("technique matrix scopes: covered and gaps hide empty columns, an empty scope says so", () => {
  assert.deepEqual(matrixModel(MATRIX, null, "covered").columns.map((c) => [c.label, c.cells.map((cell) => cell.code)]), [
    ["Initial Access", ["T1566", "T1078"]],
    ["Execution", ["T1078"]],
  ]);
  assert.deepEqual(matrixModel(MATRIX, null, "gaps").columns.map((c) => [c.label, c.cells.map((cell) => cell.code)]), [["Execution", ["T1059"]]]);
  assert.equal(matrixModel(MATRIX, "mitre-atlas", "covered").empty, true);
  assert.equal(matrixModel({ ...MATRIX, items: [] }, null, "covered").empty, true);
  assert.equal(matrixModel(MATRIX, null, "gaps").empty, false);
  // Without a reference or chains, the outcomes and their groups are the matrix.
  const flat = matrixModel({ items: [outcome("A", 1, 0, 0, { group: "G1" }), outcome("B", 0, 1, 0, { group: "G2" })] }, null, "all");
  assert.deepEqual(flat.columns.map((c) => [c.label, c.cells.map((cell) => cell.label)]), [["G1", ["A"]], ["G2", ["B"]]]);
});

test("technique accents: by success rate, slate untested", () => {
  assert.deepEqual([null, 0, 0.25, 0.5, 0.75, 0.76].map((rate) => coverageAccent(rate, "dark")), ["#5c6b7a", "#f44336", "#f44336", "#ff7043", "#ffb300", "#26a96c"]);
});

const PATH_DATA: WidgetOutcomesData = {
  chains: [{ id: "path", label: "", groups: [{ id: "init", label: "Initial Access" }, { id: "exec", label: "Execution" }] }],
  items: [
    outcome("Phishing", 2, 1, 0, { id: "ap-phish", phases: ["init"], links: ["ap-cmd", "ap-valid"] }),
    outcome("Valid Accounts", 0, 1, 0, { id: "ap-valid", phases: ["init", "exec"], links: ["ap-cmd"] }),
    outcome("Command", 1, 0, 0, { id: "ap-cmd", phases: ["exec"] }),
  ],
  timeline: { start: "2026-10-20T09:00:00Z", end: "2026-10-20T11:15:30Z" },
};

test("path graph: headers at the top, outcomes at 50 + 200x / 50 + 150y, dates under the first and last columns", () => {
  assert.deepEqual([PATH.padding, PATH.xGap, PATH.yGap, PATH.nodeWidth, PATH.arrow], [50, 200, 150, 150, 15]);
  const { nodes, maxX, maxY } = pathNodes(PATH_DATA);
  assert.deepEqual(nodes.map((node) => [node.kind, node.id, node.x, node.y]), [
    ["group", "init", 50, 0],
    ["outcome", "ap-phish-init", 50, 50],
    ["outcome", "ap-valid-init", 50, 200],
    ["group", "exec", 250, 0],
    ["outcome", "ap-valid-exec", 250, 50],
    ["outcome", "ap-cmd-exec", 250, 200],
  ]);
  // Two groups or fewer: the end date sits in the second column.
  assert.deepEqual([maxX, maxY], [1, 1]);
  const four = pathNodes({ ...PATH_DATA, chains: [{ id: "p", label: "", groups: ["a", "b", "c", "d"].map((id) => ({ id, label: id })) }] });
  assert.equal(four.maxX, 3);
});

test("path graph: a hovered node's edges reach every node of what it leads to; same-row arcs", () => {
  assert.deepEqual(pathEdges(PATH_DATA, null), []);
  assert.deepEqual(pathEdges(PATH_DATA, "ap-phish-init").map((edge) => edge.target), ["ap-cmd-exec", "ap-valid-init", "ap-valid-exec"]);
  assert.deepEqual(pathEdges(PATH_DATA, "ap-valid-exec").map((edge) => [edge.source, edge.target]), [["ap-valid-exec", "ap-cmd-exec"]]);
  assert.equal(rowArc(400, 100, 200, 104), "M 400 90 A 110.00000000000001 35 0 1 0 200 94");
  assert.equal(rowArc(200, 100, 400, 100), null);
  assert.equal(rowArc(400, 100, 200, 120), null);
});

test("path graph rates: a whole percentage, floored, as the module computes it", () => {
  assert.equal(pathRate(outcome("", 2, 1)), 0.66);
  assert.equal(pathRate(outcome("", 0, 0)), null);
});

const DOMAINS: WidgetOutcomesData = {
  reference: [
    { id: "d-end", label: "Endpoint", icon: "monitor-smartphone", parts: [{ label: "Prevention", icon: "shield-check" }] },
    { id: "d-cloud", label: "Cloud", icon: "cloud", parts: [{ label: "Prevention", icon: "shield-check" }] },
  ],
  items: [
    outcome("Endpoint", 33, 10, 2, {
      icon: "monitor-smartphone",
      parts: [
        {
          label: "Prevention", success: 30, failed: 10, pending: 2, icon: "shield-check", key: "PREVENTION", total: 42,
          rows: [{ label: "Prevented", value: 30, status: "success", key: "SUCCESS" }, { label: "Not Prevented", value: 10, status: "failed", key: "FAILED" }, { label: "Pending", value: 2, status: "pending", key: "PENDING" }],
        },
        { label: "Manual", success: 3, failed: 0, pending: 0, rows: [] },
      ],
    }),
  ],
}; // prettier-ignore

test("domain cards: every listed domain in order, scores and bands, rows as shares of their part", () => {
  const [endpoint, cloud] = domainCards(DOMAINS, "dark");
  assert.deepEqual([endpoint.label, endpoint.hasData, Math.round(endpoint.score), endpoint.color], ["Endpoint", true, 77, "rgb(2, 129, 8)"]);
  assert.deepEqual(endpoint.parts.map((part) => [part.label, part.color]), [["Prevention", "rgb(255, 216, 0)"], ["Manual", "rgb(2, 129, 8)"]]);
  assert.deepEqual(endpoint.parts[0].rows.map((row) => [row.label, Math.round(row.percentage), row.color]), [
    ["Prevented", 71, "rgb(2,129,8)"],
    ["Not Prevented", 24, "rgb(220, 81, 72)"],
    ["Pending", 5, "rgba(248,243,243,0.74)"],
  ]);
  // No data: no score, the empty colour, the placeholders in it.
  assert.deepEqual([cloud.hasData, cloud.score, cloud.color, cloud.parts.map((p) => [p.label, p.color])], [false, -1, "rgba(128, 127, 127, 0.37)", [["Prevention", "rgba(128,127,127,0.37)"]]]);
  assert.equal(domainCards(DOMAINS, "light")[0].parts[0].rows[2].color, "rgba(248,243,243,0.37)");
  assert.deepEqual([10, 25, 26, 50, 51, 75, 76].map((average) => averageColor(average, "dark")), [
    "rgb(220, 81, 72)", "rgb(220, 81, 72)", "rgb(245, 166, 35)", "rgb(245, 166, 35)", "rgb(255, 216, 0)", "rgb(255, 216, 0)", "rgb(2, 129, 8)",
  ]); // prettier-ignore
});

test("samples: three expectation types as their buckets come, and as gates in attack order", () => {
  assert.deepEqual(SAMPLE_OUTCOMES.items.map((item) => [item.label, item.success, item.failed, item.pending]), [
    ["Prevention", 58, 22, 6],
    ["Detection", 44, 38, 9],
    ["Vulnerability", 23, 17, 4],
  ]);
  assert.deepEqual(SAMPLE_GATES.items.map((item) => item.label), ["Vulnerability", "Prevention", "Detection"]);
});
