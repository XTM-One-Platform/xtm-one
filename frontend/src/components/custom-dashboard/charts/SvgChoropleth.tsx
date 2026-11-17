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
 * A choropleth on an uploaded SVG (a floor plan, a network diagram, a map of
 * one's own): each element whose id is a region's code takes that region's
 * class colour, like the atlas' countries.
 *
 * The SVG went through the template assets' sanitizer when the dashboard was
 * saved (`svg_sanitizer`), and it is never put into the page as markup here
 * either: it is parsed as data and redrawn as React elements from an
 * allowlist of shapes and geometry attributes - no script, style, link,
 * image, text node or `url(...)` survives, whatever the stored text holds.
 */
import { createElement, useMemo, type ReactNode } from "react";

import { useFormatter } from "@/i18n";
import { formatCompactNumber } from "@/lib/utils";
import type { WidgetGeoData } from "@/types";

import { choroplethClasses, classFill, regionsByCode } from "../viz/geo";
import { VIZ_EMPTY, VIZ_SURFACE } from "./viz-colors";
import { VizNote } from "./VizNote";

const SHAPES = new Set(["g", "path", "rect", "circle", "ellipse", "polygon", "polyline", "line"]);
/** Geometry only; a value is a number list or path data, checked below. */
const GEOMETRY: Record<string, string> = {
  d: "d",
  x: "x",
  y: "y",
  width: "width",
  height: "height",
  cx: "cx",
  cy: "cy",
  r: "r",
  rx: "rx",
  ry: "ry",
  points: "points",
  x1: "x1",
  y1: "y1",
  x2: "x2",
  y2: "y2",
  transform: "transform",
};
const NUMBERS = /^[-+0-9.eE,\s]*$/;
const PATH = /^[MmLlHhVvCcSsQqTtAaZz0-9.eE,\s+-]*$/;
const TRANSFORM = /^(\s*(matrix|translate|scale|rotate|skewX|skewY)\(\s*[-+0-9.eE,\s]*\)\s*,?)*\s*$/;
const MAX_ELEMENTS = 5_000;
const MAX_DEPTH = 24;

function attributeOk(name: string, value: string): boolean {
  if (value.length > 100_000) return false;
  if (name === "d") return PATH.test(value);
  if (name === "transform") return TRANSFORM.test(value);
  return NUMBERS.test(value);
}

interface Parsed {
  viewBox: string;
  nodes: ReactNode;
  regions: number;
}

function redraw(svg: string, paint: (id: string) => { fill: string; title?: string } | null): Parsed | null {
  if (typeof DOMParser === "undefined") return null;
  const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
  const root = doc.documentElement;
  if (!root || root.nodeName.toLowerCase() !== "svg" || doc.getElementsByTagName("parsererror").length) return null;
  const viewBox = root.getAttribute("viewBox") ?? "";
  const width = Number(root.getAttribute("width"));
  const height = Number(root.getAttribute("height"));
  const box = /^\s*[-+0-9.eE]+([\s,]+[-+0-9.eE]+){3}\s*$/.test(viewBox)
    ? viewBox.trim()
    : Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0
      ? `0 0 ${width} ${height}`
      : null;
  if (!box) return null;
  let count = 0;
  let regions = 0;
  const walk = (element: Element, depth: number, key: string): ReactNode => {
    const tag = element.localName;
    if (!SHAPES.has(tag) || depth > MAX_DEPTH || count >= MAX_ELEMENTS) return null;
    count += 1;
    const props: Record<string, unknown> = { key };
    for (const { name, value } of Array.from(element.attributes)) {
      if (GEOMETRY[name] && attributeOk(name, value)) props[GEOMETRY[name]] = value;
    }
    const id = element.getAttribute("id");
    const painted = id ? paint(id) : null;
    if (painted) regions += 1;
    if (tag !== "g") {
      props.style = painted
        ? { fill: painted.fill, stroke: VIZ_SURFACE, strokeWidth: 1, vectorEffect: "non-scaling-stroke" }
        : { fill: VIZ_EMPTY, stroke: VIZ_SURFACE, strokeWidth: 1, vectorEffect: "non-scaling-stroke" };
      if (painted) props["data-region"] = id;
    }
    const children = Array.from(element.children).map((child, index) => walk(child, depth + 1, `${key}.${index}`));
    if (painted?.title) children.unshift(createElement("title", { key: "title" }, painted.title));
    return createElement(tag, props, ...children);
  };
  const nodes = Array.from(root.children).map((child, index) => walk(child, 1, String(index)));
  return { viewBox: box, nodes, regions };
}

export function SvgChoropleth({ data, svg, height }: { data: WidgetGeoData; svg: string; height: number }) {
  const { t_i18n } = useFormatter();
  const parsed = useMemo(() => {
    const regions = regionsByCode(data, false);
    const classes = choroplethClasses(data.regions.map((r) => r.value));
    return redraw(svg, (id) => {
      const region = regions.get(id);
      return region ? { fill: classFill(classes.classOf(region.value)), title: `${region.label}: ${formatCompactNumber(region.value)}` } : null;
    });
  }, [data, svg]);
  if (!parsed) return <VizNote height={height}>{t_i18n("The SVG of this choropleth cannot be drawn.")}</VizNote>;
  return (
    <svg
      className="w-full"
      height={height}
      viewBox={parsed.viewBox}
      preserveAspectRatio="xMidYMid meet"
      role="img"
      aria-label={t_i18n("{count, plural, one {Map of # place} other {Map of # places}}", { count: parsed.regions })}
      data-viz="choroplethsvg"
    >
      {parsed.nodes}
    </svg>
  );
}
