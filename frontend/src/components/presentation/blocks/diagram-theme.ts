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
 * How the deck runs Mermaid: the configuration that never changes
 * (`MERMAID_CONFIG`) and the deck theme as Mermaid takes it.
 *
 * Mermaid draws from its own theme variables and its own stylesheet, both
 * baked into the SVG it returns, so the deck's tokens are read off the host's
 * resolved CSS custom properties at render time (`deckDiagramTheme`) and
 * reach the drawing three ways:
 *
 * - `themeVariables`: the palette, wherever Mermaid's base theme has a
 *   variable for it - node fill and border, edge and arrowhead colour, the
 *   edge-label background, the fonts, the pie slices.
 * - `themeCSS`: what the base theme has no variable for - a node label in the
 *   heading font, the deck's border weight on every node, an opaque pill
 *   behind an edge label, the arrowheads in the accent. Mermaid compiles it
 *   under the SVG's own id after its stylesheet, so it wins by source order
 *   and travels with the SVG into the export.
 * - geometry (`fitDeckGeometry`): what no stylesheet can set on a drawing
 *   Mermaid has laid out - the corner radius of a rectangular node, the padding
 *   and round ends of an edge-label pill - written as attributes on the
 *   rendered SVG, after layout and before display. Attributes, not CSS
 *   geometry properties: every SVG renderer honours them.
 *
 * Every colour is painted first (`paintedColor`): Mermaid parses colours
 * itself and throws on `color-mix()`, which is what the dark scheme's surface
 * and both schemes' line are. Only a `rect` node gets the radius: a stadium, a
 * cylinder, a circle, a diamond are their own shapes.
 *
 * Mermaid has no minimum node width: a flowchart node is its label plus the
 * configured padding (`drawRect` takes `node.width` as a floor, but nothing
 * public sets it before layout - the layout loader that could is reachable
 * only through Mermaid's private dagre chunk). Widening a node after layout is
 * not an option either: dagre has already routed every edge to the node's
 * border.
 */

type MermaidConfig = Parameters<(typeof import("mermaid").default)["initialize"]>[0];
type ThemeVariables = Record<string, string | boolean>;

/**
 * The non-theme half of the Mermaid configuration: strict mode (labels are
 * text, no HTML, no click), SVG-only labels, the deck's spacing.
 *
 * `mermaid.initialize` REPLACES the site config rather than merging into the
 * previous call, so every `initialize` (one per render, to pick up the deck's
 * resolved theme) must carry the whole thing. Passing only `themeVariables`
 * silently reset `htmlLabels` to its default (`true`): Mermaid then emitted the
 * labels as `<foreignObject>` HTML, DOMPurify's SVG profile stripped them, and
 * every node rendered as an empty box.
 */
export const MERMAID_CONFIG: MermaidConfig = {
  startOnLoad: false,
  securityLevel: "strict",
  theme: "base",
  // Text labels only: keeps the SVG self-contained (no <foreignObject>), which
  // is also what makes it embeddable in the PPTX/PDF export.
  htmlLabels: false,
  // Mermaid's default spacing (50) was set for 16px text; the deck's 26px type
  // takes it to 80, which is also what keeps a padded edge-label pill
  // (`fitDeckGeometry`) clear of the nodes on either side of it. The drawing's
  // own padding (8 by default) grows past the pill padding, so a pill at the
  // edge of the drawing stays inside its viewBox.
  flowchart: { htmlLabels: false, useMaxWidth: true, nodeSpacing: 80, rankSpacing: 80, diagramPadding: 16 },
  sequence: { useMaxWidth: true },
  gantt: { useMaxWidth: true },
  journey: { useMaxWidth: true },
  pie: { useMaxWidth: true },
  // Fixed fonts/sizes on the 1920px canvas; the deck scales the whole thing.
  fontFamily: "var(--deck-body-font)",
};

/** What `fitDeckGeometry` draws, in Mermaid's own pixels (the 1920px canvas). */
export interface DeckDiagramGeometry {
  /** Corner radius of a rectangular node: `--deck-radius`. */
  radius: number;
  /** Horizontal padding of an edge-label pill, each side. */
  pillPaddingX: number;
  /** Vertical padding of an edge-label pill, each side. */
  pillPaddingY: number;
}

/**
 * An edge label's pill: the architecture block's (`ARCH.EDGE_LABEL_PAD`, a
 * 34px pill around 19px text) - here around Mermaid's own 2px of padding on
 * the `--deck-fs-xs` text, so the two kinds of figure label alike.
 */
const PILL_PADDING_X = 14;
const PILL_PADDING_Y = 4;

interface DeckDiagramTheme {
  themeVariables: ThemeVariables;
  themeCSS: string;
  geometry: DeckDiagramGeometry;
}

let pixelProbe: CanvasRenderingContext2D | null | undefined;

/**
 * The colour as the browser paints it, written `rgb()` / `rgba()`. Mermaid
 * parses colours itself and throws on `color-mix()` (the dark scheme's
 * surface) and on the `color(srgb ...)` a computed style turns it into.
 */
function paintedColor(host: HTMLElement, value: string, fallback: string): string {
  pixelProbe ??= document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  if (!pixelProbe) return /^(#|rgba?\(|hsla?\()/i.test(value) ? value : fallback;
  const probe = document.createElement("span");
  probe.style.color = value;
  if (!probe.style.color) return fallback;
  host.appendChild(probe);
  const computed = getComputedStyle(probe).color;
  probe.remove();
  pixelProbe.clearRect(0, 0, 1, 1);
  pixelProbe.fillStyle = computed;
  pixelProbe.fillRect(0, 0, 1, 1);
  const [r, g, b, a] = pixelProbe.getImageData(0, 0, 1, 1).data;
  return a === 255 ? `rgb(${r}, ${g}, ${b})` : `rgba(${r}, ${g}, ${b}, ${(a / 255).toFixed(3)})`;
}

/** The pixels of a `<length>` token (`12px`), or the fallback when it is not one. */
function lengthPx(value: string, fallback: number): number {
  const match = /^(-?\d*\.?\d+)px$/.exec(value);
  return match ? Number.parseFloat(match[1]) : fallback;
}

/** The deck theme, read from the host's resolved CSS custom properties. */
export function deckDiagramTheme(host: HTMLElement): DeckDiagramTheme {
  const css = getComputedStyle(host);
  const read = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
  const color = (name: string, fallback: string) => paintedColor(host, read(name, fallback), fallback);
  const fg = color("--deck-fg", "#ffffff");
  const bg = color("--deck-bg", "#0a0f1e");
  const accent = color("--deck-accent", "#0fbcff");
  const muted = color("--deck-muted", "#a0a4b8");
  const surface = color("--deck-surface-solid", bg);
  const line = color("--deck-line", muted);
  const headingFont = read("--deck-heading-font", "Geologica, sans-serif");
  const bodyFont = read("--deck-body-font", "IBM Plex Sans, sans-serif");
  const border = lengthPx(read("--deck-border", "2px"), 2);
  const labelSize = lengthPx(read("--deck-fs-xs", "20px"), 20);
  const geometry: DeckDiagramGeometry = {
    radius: lengthPx(read("--deck-radius", "12px"), 12),
    pillPaddingX: PILL_PADDING_X,
    pillPaddingY: PILL_PADDING_Y,
  };
  const themeVariables: ThemeVariables = {
    darkMode: read("--deck-scheme", "dark") === "dark",
    background: bg,
    fontFamily: bodyFont,
    fontSize: "26px",
    primaryColor: surface,
    primaryTextColor: fg,
    primaryBorderColor: accent,
    secondaryColor: surface,
    secondaryTextColor: fg,
    secondaryBorderColor: muted,
    tertiaryColor: surface,
    tertiaryTextColor: fg,
    tertiaryBorderColor: muted,
    lineColor: muted,
    arrowheadColor: accent,
    textColor: fg,
    mainBkg: surface,
    nodeBorder: line,
    clusterBkg: surface,
    clusterBorder: muted,
    titleColor: fg,
    edgeLabelBackground: surface,
    actorBkg: surface,
    actorBorder: accent,
    actorTextColor: fg,
    signalColor: fg,
    signalTextColor: fg,
    labelBoxBkgColor: surface,
    labelBoxBorderColor: accent,
    labelTextColor: fg,
    noteBkgColor: surface,
    noteBorderColor: muted,
    noteTextColor: fg,
    pie1: accent,
    pie2: color("--deck-info", "#5b8def"),
    pie3: color("--deck-success", "#27c28a"),
    pie4: color("--deck-warning", "#f2a93b"),
    pie5: color("--deck-error", "#f0616b"),
    pie6: muted,
    pieTitleTextColor: fg,
    pieSectionTextColor: fg,
    pieLegendTextColor: fg,
    pieStrokeColor: bg,
    pieOuterStrokeColor: bg,
  };
  // Mermaid nests every rule under the SVG's id, after its own stylesheet,
  // and measures every label with the stylesheet in place, so a font set here
  // is the font the layout was made for. A label's inner tspans carry a
  // `font-weight="normal"` attribute, which beats an inherited weight: the
  // weight is set on them, and a `**bold**` run keeps its own.
  const themeCSS = [
    `.node .label, .cluster-label { font-family: ${headingFont}; }`,
    `.node .label tspan[font-weight="normal"], .cluster-label tspan[font-weight="normal"] { font-weight: 600; }`,
    `.node rect, .node circle, .node ellipse, .node polygon, .node path { stroke-width: ${border}px; }`,
    `.flowchart-link { stroke-width: ${border}px; }`,
    `.edgeLabel .label text { font-size: ${labelSize}px; }`,
    `.edgeLabel tspan[font-weight="normal"] { font-weight: 500; }`,
    `.edgeLabel rect { opacity: 1; fill: ${surface}; stroke: ${line}; stroke-width: ${border}px; }`,
    `.marker { fill: ${accent}; stroke: ${accent}; }`,
    `.marker.cross { stroke: ${accent}; }`,
  ].join("\n");
  return { themeVariables, themeCSS, geometry };
}

const px = (element: Element, name: string): number => Number.parseFloat(element.getAttribute(name) ?? "") || 0;

/**
 * Writes the deck's geometry onto a flowchart Mermaid has laid out: every
 * rectangular node rounded to the deck's radius (a stadium, a circle, a
 * diamond, a cylinder keep their shape), every edge label's background grown
 * by the pill padding around the text Mermaid measured and given round ends.
 * Dagre reserves `rankSpacing` on either side of a label, so the padding
 * never reaches a node.
 */
export function fitDeckGeometry(root: SVGSVGElement, geometry: DeckDiagramGeometry): void {
  for (const rect of root.querySelectorAll(".node > rect.label-container")) {
    rect.setAttribute("rx", String(geometry.radius));
    rect.setAttribute("ry", String(geometry.radius));
  }
  for (const rect of root.querySelectorAll(".edgeLabel rect.background[width]")) {
    const height = px(rect, "height") + 2 * geometry.pillPaddingY;
    rect.setAttribute("x", String(px(rect, "x") - geometry.pillPaddingX));
    rect.setAttribute("y", String(px(rect, "y") - geometry.pillPaddingY));
    rect.setAttribute("width", String(px(rect, "width") + 2 * geometry.pillPaddingX));
    rect.setAttribute("height", String(height));
    rect.setAttribute("rx", String(height / 2));
    rect.setAttribute("ry", String(height / 2));
  }
}
