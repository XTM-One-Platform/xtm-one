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
 * What a map's layers decide from a `geo` answer, apart from the projection:
 *
 * - the **choropleth** gives each region a class of `CLASSES` equal steps
 *   between the least and the largest value (`choroplethClasses`), drawn as
 *   that many shades of the accent, and a legend of the steps;
 * - the **bubble** layer sizes each point by its value, its AREA in
 *   proportion (radius on a square-root scale, `bubbleRadius`);
 * - the **marker** layer draws each point as a pin of one size.
 *
 * A region's code is matched without case (`fr` is France on the atlas); on
 * an uploaded SVG, it is the element's id as written. Which layers a map
 * draws is its `layers` option (`mapLayers`), else what the data holds.
 *
 * Pure, type-only imports: runs on `node --test`.
 */
import type { WidgetGeoData } from "@/types";

export const CLASSES = 5;
export const MIN_BUBBLE = 3;
export const MAX_BUBBLE = 24;

export type MapLayer = "choropleth" | "bubble" | "marker";
const MAP_LAYERS: readonly MapLayer[] = ["choropleth", "bubble", "marker"];

/** The map's layers, in drawing order: the asked ones it knows, else a choropleth for regions and bubbles for points. */
export function mapLayers(options: Record<string, unknown>, data: WidgetGeoData): MapLayer[] {
  const asked = Array.isArray(options.layers)
    ? options.layers
        .map((layer) => (typeof layer === "string" ? layer : typeof layer === "object" && layer !== null ? (layer as { type?: unknown }).type : null))
        .filter((layer): layer is MapLayer => typeof layer === "string" && (MAP_LAYERS as readonly string[]).includes(layer))
    : [];
  if (asked.length > 0) return MAP_LAYERS.filter((layer) => asked.includes(layer));
  const layers: MapLayer[] = [];
  if (data.regions.length > 0) layers.push("choropleth");
  if (data.points.length > 0) layers.push("bubble");
  return layers;
}

export interface Classes {
  /** The class of a value: 0 (least) to `CLASSES - 1`. */
  classOf: (value: number) => number;
  /** The lower bound of each class, then the upper bound of the last. */
  breaks: number[];
}

export function choroplethClasses(values: readonly number[], count = CLASSES): Classes {
  if (values.length === 0) return { classOf: () => 0, breaks: [] };
  const min = Math.min(...values);
  const max = Math.max(...values);
  if (max === min) return { classOf: () => count - 1, breaks: [min, max] };
  const step = (max - min) / count;
  return {
    classOf: (value) => Math.min(count - 1, Math.max(0, Math.floor((value - min) / step))),
    breaks: Array.from({ length: count + 1 }, (_, i) => min + i * step),
  };
}

/** A class's shade: the accent mixed into the surface, deeper for a higher class. */
export function classFill(index: number, count = CLASSES): string {
  const share = Math.round(25 + (75 * index) / Math.max(1, count - 1));
  return `color-mix(in oklab, var(--color-accent) ${share}%, var(--bg-elevation-default))`;
}

/** A bubble's radius: its area proportional to its value; no value is the least bubble. */
export function bubbleRadius(value: number | null, max: number, largest = MAX_BUBBLE): number {
  if (value === null || !(value > 0) || !(max > 0)) return MIN_BUBBLE;
  return Math.max(MIN_BUBBLE, Math.sqrt(value / max) * largest);
}

/** The regions by code, matched without case. */
export function regionsByCode(data: WidgetGeoData, caseless: boolean): Map<string, WidgetGeoData["regions"][number]> {
  return new Map(data.regions.map((region) => [caseless ? region.code.toUpperCase() : region.code, region]));
}

// ── A map's look: regions by level, its basemap, its markers, its view ───────
//
// `regionColors: "level"` colours each region by its `level` (0-9) on the
// threat scale (`charts/palettes.threatColor`: level 0 is drawn as level 5),
// filled at 10% in the dark theme and 50% in the light one, outlined 1 px in
// the same colour; `basemap: "contrast"` draws the land, the sea and the
// borders in a map's night (or day) colours; the markers may be an icon
// (`markerIcon`) of `markerSize` px in `markerColor`; and `zoom` with a
// centre opens the map as a Web Mercator view of 512 px tiles does at that
// zoom (OpenCTI's map: `GeoMap`, `LocationMiniMapTargets`).

const REGION_COLORS = ["classes", "level"] as const;
type RegionColors = (typeof REGION_COLORS)[number];
const BASEMAPS = ["surface", "contrast"] as const;
type Basemap = (typeof BASEMAPS)[number];

/** The contrast basemap's colours by theme: land, sea, borders. */
export const CONTRAST_BASEMAP = {
  dark: { land: "#1c2f4a", sea: "#0b1422", border: "#0fbcff" },
  light: { land: "#dce0fe", sea: "#ffffff", border: "#001bda" },
} as const;
/** A levelled region's fill opacity by theme. */
export const LEVEL_FILL_OPACITY = { dark: 0.1, light: 0.5 } as const;
/** The width of a Web Mercator world at zoom 0 (px): a 512 px tile. */
export const TILE_SIZE = 512;

export interface MapLook {
  regionColors: RegionColors;
  basemap: Basemap;
  markerColor: string | undefined;
  markerSize: number;
  markerIcon: string | undefined;
  /** A Web Mercator view, when the map names a zoom; else the region fitted in the box. */
  view: { zoom: number; center: [number, number]; onFirstMarker: boolean } | null;
}

const HEX = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;
const finite = (value: unknown, min: number, max: number): number | undefined =>
  typeof value === "number" && Number.isFinite(value) && value >= min && value <= max ? value : undefined;

export function readMapLook(options: Record<string, unknown>): MapLook {
  const zoom = finite(options.zoom, 0, 20);
  const lat = finite(options.centerLat, -90, 90);
  const lon = finite(options.centerLng, -180, 180);
  return {
    regionColors: (REGION_COLORS as readonly unknown[]).includes(options.regionColors) ? (options.regionColors as RegionColors) : "classes",
    basemap: (BASEMAPS as readonly unknown[]).includes(options.basemap) ? (options.basemap as Basemap) : "surface",
    markerColor: typeof options.markerColor === "string" && HEX.test(options.markerColor) ? options.markerColor : undefined,
    markerSize: Math.round(finite(options.markerSize, 8, 40) ?? 14),
    markerIcon: typeof options.markerIcon === "string" && /^[a-z0-9-]{1,60}$/.test(options.markerIcon) ? options.markerIcon : undefined,
    view: zoom === undefined ? null : { zoom, center: [lon ?? 0, lat ?? 0], onFirstMarker: options.centerOnFirstMarker === true },
  };
}

/** Where a view opens: the first point when it asks to and there is one, else its centre ([lon, lat]). */
export function viewCenter(view: NonNullable<MapLook["view"]>, data: WidgetGeoData): [number, number] {
  const first = view.onFirstMarker ? data.points[0] : undefined;
  return first ? [first.lon, first.lat] : view.center;
}

/** The scale and translation of a Mercator projection (d3's) showing `zoom` around `center` in a `width` x `height` box. */
export function mercatorFrame(zoom: number, center: [number, number], width: number, height: number): { scale: number; translate: [number, number] } {
  // A world `TILE_SIZE * 2^zoom` px wide is a sphere of that circumference: d3's scale is its radius.
  const scale = (TILE_SIZE * 2 ** zoom) / (2 * Math.PI);
  const lambda = (center[0] * Math.PI) / 180;
  const phi = (Math.max(-85.0511, Math.min(85.0511, center[1])) * Math.PI) / 180;
  const x = scale * lambda;
  const y = -scale * Math.log(Math.tan(Math.PI / 4 + phi / 2));
  return { scale, translate: [width / 2 - x, height / 2 - y] };
}
