import { test } from "vitest";
import assert from "node:assert/strict";

import { A2A_EDGE_CONFIG } from "./agent-graph-a2a";
import { ALL_HANDOVER_TYPES_CONFIG, HANDOVER_TYPE_CONFIG } from "./constants";
import { dashAnimation, dashKeyframes, dashPeriod } from "./agent-graph-dash";

const EVERY_TYPE = [
  ...Object.entries(HANDOVER_TYPE_CONFIG),
  ["all", ALL_HANDOVER_TYPES_CONFIG] as const,
  ...Object.entries(A2A_EDGE_CONFIG),
];

/** The offset one cycle moves the dash by, read from the keyframes it names. */
function cycleOffset(animation: string, keyframes: string): number {
  const name = animation.split(" ")[0];
  const rule = keyframes.split("\n").find((line) => line.startsWith(`@keyframes ${name}{`));
  assert.ok(rule, `no @keyframes ${name}`);
  return Math.abs(Number(/stroke-dashoffset:(-?[\d.]+)/.exec(rule!)![1]));
}

test("the period of a dash array, odd lists doubled as SVG draws them", () => {
  assert.equal(dashPeriod("6 4"), 10);
  assert.equal(dashPeriod("2 3"), 5);
  assert.equal(dashPeriod("8 3 2 3"), 16);
  assert.equal(dashPeriod("5"), 10);
  assert.equal(dashPeriod("1,2,3"), 12);
  assert.equal(dashPeriod(null), null);
  assert.equal(dashPeriod(""), null);
  assert.equal(dashPeriod("0 0"), null);
});

test("every dashed edge type moves its dash by a whole period per cycle, so the loop never snaps back (#12382)", () => {
  const keyframes = dashKeyframes(EVERY_TYPE.map(([, c]) => c.dash));
  for (const [type, config] of EVERY_TYPE) {
    const period = dashPeriod(config.dash);
    for (const live of [false, true]) {
      const animation = dashAnimation(config.dash, live);
      if (period === null) {
        assert.equal(animation, undefined, `${type}: a solid edge has no dash to march`);
        continue;
      }
      const offset = cycleOffset(animation!, keyframes);
      const periods = offset / period;
      assert.ok(Math.abs(periods - Math.round(periods)) < 1e-9 && periods >= 1, `${type}: one cycle moves ${offset}, the dash period is ${period}`);
    }
  }
});

test("the A2A liaison dash is one of the patterns animated without a jump", () => {
  const animation = dashAnimation(A2A_EDGE_CONFIG.a2a_inbound.dash, false)!;
  const keyframes = dashKeyframes([A2A_EDGE_CONFIG.a2a_inbound.dash]);
  assert.equal(cycleOffset(animation, keyframes), 16);
});

test("every pattern marches at the speed the map always had: 10 units per 3 s at rest, per 1.2 s live", () => {
  for (const dash of ["6 4", "2 3", "8 3 2 3"]) {
    const period = dashPeriod(dash)!;
    for (const [live, seconds10] of [[false, 3], [true, 1.2]] as const) {
      const seconds = Number(/ ([\d.]+)s /.exec(dashAnimation(dash, live)!)![1]);
      assert.ok(Math.abs(period / seconds - 10 / seconds10) < 1e-6, `${dash} live=${live}: ${period} units in ${seconds}s`);
    }
  }
});

test("one keyframe per distinct period", () => {
  const rules = dashKeyframes(["6 4", "4 6", "2 3", null, "8 3 2 3"]).split("\n");
  assert.equal(rules.length, 3);
});
