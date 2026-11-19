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
 * A chart of the Results page saved as a stand-alone SVG file (#2): its design-token
 * colours resolved to their current values, its text given the font it is drawn with, and
 * the card's background behind it, so the file reads the same outside the app.
 */

import { saveBlobAs } from "@/lib/utils";
import { resolveCssVars } from "./evaluation-results.ts";

/** The first opaque background colour from `el` up. */
function backgroundOf(el: Element | null): string {
  for (let node = el; node; node = node.parentElement) {
    const bg = getComputedStyle(node).backgroundColor;
    if (bg && bg !== "transparent" && !/rgba\([^)]*,\s*0\)$/.test(bg)) return bg;
  }
  return "";
}

/** The SVG's markup, standing alone. */
function svgMarkup(svg: SVGSVGElement): string {
  const clone = svg.cloneNode(true) as SVGSVGElement;
  const from = svg.querySelectorAll("text, tspan");
  const to = clone.querySelectorAll("text, tspan");
  from.forEach((el, i) => {
    const style = getComputedStyle(el);
    const target = to[i];
    target.setAttribute("font-family", style.fontFamily);
    target.setAttribute("font-size", style.fontSize);
    target.setAttribute("font-weight", style.fontWeight);
    target.removeAttribute("class");
  });
  clone.removeAttribute("class");
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  const box = svg.viewBox.baseVal;
  if (box && box.width) {
    clone.setAttribute("width", String(box.width));
    clone.setAttribute("height", String(box.height));
  }
  const bg = backgroundOf(svg);
  if (bg) {
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("width", "100%");
    rect.setAttribute("height", "100%");
    rect.setAttribute("fill", bg);
    clone.insertBefore(rect, clone.firstChild);
  }
  const computed = getComputedStyle(svg);
  return resolveCssVars(new XMLSerializer().serializeToString(clone), (name) => computed.getPropertyValue(name).trim());
}

/** Saves the first chart inside `container` as `filename`; false when there is none. */
export function saveChartSvg(container: Element | null, filename: string): boolean {
  const svg = container?.querySelector("svg");
  if (!svg) return false;
  saveBlobAs(new Blob([svgMarkup(svg)], { type: "image/svg+xml;charset=utf-8" }), filename);
  return true;
}
