/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * Flowchart glyphs for utility nodes — the React Flow "shapes" look.
 *
 * A shaped utility draws as a compact SVG figure (icon centred, name under
 * the glyph) instead of the standard card. Presentation only: the walker
 * never reads `config.shape`, and the builder strips it from the semantic
 * comparison like node positions.
 *
 * Defaults follow flowchart conventions — mirrored from the server catalog's
 * `DEFAULT_SHAPES` so the canvas and the drawer agree before any save.
 */

export type NodeShape =
  | "rectangle"
  | "diamond"
  | "circle"
  | "hexagon"
  | "parallelogram"
  | "pill";

const DEFAULT_NODE_SHAPES: Record<string, NodeShape> = {
  "util.primitive": "pill",
  "util.stop": "circle",
  "util.loop": "hexagon",
  "util.while": "hexagon",
  "util.gate": "diamond",
  "util.expression": "parallelogram",
  "util.merge": "diamond",
  "util.delay": "pill",
  "util.wait_event": "pill",
};

/** Glyph footprint per shape — sized so two-output nodes stay wireable. */
export const SHAPE_SIZES: Record<Exclude<NodeShape, "rectangle">, [number, number]> = {
  diamond: [148, 88],
  circle: [96, 96],
  hexagon: [156, 76],
  parallelogram: [156, 64],
  pill: [148, 60],
};

/** The glyph outline as an SVG path (drawn inset so the stroke never clips). */
export function shapePath(
  shape: Exclude<NodeShape, "rectangle">,
  w: number,
  h: number,
): string {
  const i = 1.5; // stroke inset
  const W = w - i;
  const H = h - i;
  switch (shape) {
    case "diamond":
      return `M ${w / 2} ${i} L ${W} ${h / 2} L ${w / 2} ${H} L ${i} ${h / 2} Z`;
    case "circle": {
      const rx = (w - 2 * i) / 2;
      const ry = (h - 2 * i) / 2;
      return `M ${i} ${h / 2} a ${rx} ${ry} 0 1 0 ${rx * 2} 0 a ${rx} ${ry} 0 1 0 ${-rx * 2} 0 Z`;
    }
    case "hexagon": {
      const inset = Math.min(w * 0.16, h * 0.7);
      return `M ${i + inset} ${i} H ${W - inset} L ${W} ${h / 2} L ${W - inset} ${H} H ${i + inset} L ${i} ${h / 2} Z`;
    }
    case "parallelogram": {
      const skew = Math.min(w * 0.14, 26);
      return `M ${i + skew} ${i} H ${W} L ${W - skew} ${H} H ${i} Z`;
    }
    case "pill": {
      const r = (h - 2 * i) / 2;
      return `M ${i + r} ${i} H ${W - r} A ${r} ${r} 0 0 1 ${W - r} ${H} H ${i + r} A ${r} ${r} 0 0 1 ${i + r} ${i} Z`;
    }
  }
}

/** The shape a node draws as — explicit config first, then the kind's default. */
export function resolveNodeShape(
  kind: string,
  category: string | undefined,
  configShape: unknown,
): NodeShape {
  if (category !== "utilities") return "rectangle";
  if (
    typeof configShape === "string" &&
    configShape in SHAPE_SIZES
  ) {
    return configShape as NodeShape;
  }
  if (configShape === "rectangle") return "rectangle";
  return DEFAULT_NODE_SHAPES[kind] ?? "rectangle";
}
