import { test } from "vitest";
import assert from "node:assert/strict";

import { ALL_HANDOVER_TYPES_CONFIG, HANDOVER_TYPE_CONFIG } from "./constants";
import {
  contrastOnCanvas,
  DARK_CANVAS,
  DARK_CANVAS_PAINTED,
  DARK_TOKEN_INK,
  EDGE_MIN_CONTRAST,
  EDGE_REST_OPACITY,
  edgeInk,
  LIGHT_CANVAS,
} from "./agent-graph-ink";

const EVERY_TYPE = [...Object.entries(HANDOVER_TYPE_CONFIG), ["all", ALL_HANDOVER_TYPES_CONFIG] as const];

test("contrast: black on white is 21:1, a colour on itself 1:1", () => {
  assert.ok(Math.abs(contrastOnCanvas("#000000", 1, "#FFFFFF") - 21) < 1e-9);
  assert.ok(Math.abs(contrastOnCanvas("#94a3b8", 1, "#94a3b8") - 1) < 1e-9);
});

test("the light canvas is the map card's surface, white", () => {
  assert.equal(LIGHT_CANVAS, "#FFFFFF");
});

test("the old light edge (slate 400 at 0.55) was under 3:1", () => {
  assert.ok(contrastOnCanvas("#94a3b8", 0.55, LIGHT_CANVAS) < 2);
});

test("every handover edge keeps 3:1 on the light canvas at rest", () => {
  for (const [type, config] of EVERY_TYPE) {
    const ink = edgeInk(config, true);
    const ratio = contrastOnCanvas(ink, EDGE_REST_OPACITY.light, LIGHT_CANVAS);
    assert.ok(ratio >= EDGE_MIN_CONTRAST, `${type}: ${ink} at ${EDGE_REST_OPACITY.light} is ${ratio.toFixed(2)}:1`);
  }
});

test("the old dark rest opacity left the transfer edge under 3:1", () => {
  assert.ok(contrastOnCanvas("#a78bfa", 0.55, DARK_CANVAS) < EDGE_MIN_CONTRAST);
});

test("every handover edge keeps 3:1 on the dark canvas at rest, token-drawn ones included", () => {
  // Delegate and consult draw in theme tokens (var(...)): measured at the
  // dark values the tokens resolve to; a token without a value fails here.
  for (const [type, config] of EVERY_TYPE) {
    const drawn = edgeInk(config, false);
    const ink = drawn.startsWith("#") ? drawn : DARK_TOKEN_INK[drawn];
    assert.ok(ink, `${type}: no dark value for ${drawn} in DARK_TOKEN_INK`);
    for (const canvas of [DARK_CANVAS, DARK_CANVAS_PAINTED]) {
      const ratio = contrastOnCanvas(ink, EDGE_REST_OPACITY.dark, canvas);
      assert.ok(ratio >= EDGE_MIN_CONTRAST, `${type}: ${ink} at ${EDGE_REST_OPACITY.dark} on ${canvas} is ${ratio.toFixed(2)}:1`);
    }
  }
});

test("the round 1 dark rest opacity left consult under 3:1", () => {
  assert.ok(contrastOnCanvas(DARK_TOKEN_INK["var(--color-feedback-success-primary)"], 0.62, DARK_CANVAS_PAINTED) < EDGE_MIN_CONTRAST);
});

test("the dark canvas keeps its own edge colours", () => {
  assert.equal(edgeInk(HANDOVER_TYPE_CONFIG.transfer, false), "#a78bfa");
});

// CIE76 distance of two sRGB hex colours (D65), for the ink-pair tests.
function deltaE76(a: string, b: string): number {
  const lab = (hex: string) => {
    const h = hex.replace("#", "");
    const [r, g, bl] = [0, 2, 4].map((i) => {
      const v = parseInt(h.slice(i, i + 2), 16) / 255;
      return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
    });
    const f = (t: number) => (t > 216 / 24389 ? Math.cbrt(t) : (24389 / 27 * t + 16) / 116);
    const x = f((0.4124 * r + 0.3576 * g + 0.1805 * bl) / 0.95047);
    const y = f(0.2126 * r + 0.7152 * g + 0.0722 * bl);
    const z = f((0.0193 * r + 0.1192 * g + 0.9505 * bl) / 1.08883);
    return [116 * y - 16, 500 * (x - y), 200 * (y - z)];
  };
  const [p, q] = [lab(a), lab(b)];
  return Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]);
}

test("the old XTM One dark delegate ink sat 8 dE76 from transfer", () => {
  assert.ok(deltaE76("#a47af0", HANDOVER_TYPE_CONFIG.transfer.edgeColor) < 10);
});

test("delegate and transfer inks stay apart on both canvases, in every platform mode", () => {
  // Delegate is a fixed ink, never the accent token, whose value follows the mode.
  assert.ok(HANDOVER_TYPE_CONFIG.delegate.edgeColor.startsWith("#"));
  for (const isLight of [false, true]) {
    const d = deltaE76(edgeInk(HANDOVER_TYPE_CONFIG.delegate, isLight), edgeInk(HANDOVER_TYPE_CONFIG.transfer, isLight));
    assert.ok(d >= 40, `${isLight ? "light" : "dark"}: delegate vs transfer ${d.toFixed(1)} dE76`);
  }
});

test("each handover type has its own pattern: dash, or ink where two are solid", () => {
  const types = Object.entries(HANDOVER_TYPE_CONFIG);
  for (let i = 0; i < types.length; i += 1) {
    for (let j = i + 1; j < types.length; j += 1) {
      const [a, ca] = types[i];
      const [b, cb] = types[j];
      if (ca.dash !== cb.dash) continue;
      for (const isLight of [false, true]) {
        const d = deltaE76(edgeInk(ca, isLight), edgeInk(cb, isLight));
        assert.ok(d >= 40, `${a} and ${b} share the dash ${ca.dash} and sit ${d.toFixed(1)} dE76 apart`);
      }
    }
  }
  assert.equal(HANDOVER_TYPE_CONFIG.delegate.dash, null);
  assert.equal(HANDOVER_TYPE_CONFIG.transfer.dash, "6 4");
  assert.equal(HANDOVER_TYPE_CONFIG.consult.dash, "2 3");
});
