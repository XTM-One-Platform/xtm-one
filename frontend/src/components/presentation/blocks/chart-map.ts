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
 * The room of a `map` chart: where each country is drawn, in which
 * colour, which ones are marked, and which carry their value. Pure - no
 * React, no DOM - so the drawing (`MapCanvas.tsx`), the facts it publishes and
 * the tests all read ONE decision, as `chart-layout.ts` is for axis charts.
 *
 * Shapes: Natural Earth's 1:50m countries (`world-atlas`), keyed by ISO
 * 3166-1 numeric code and read here by alpha-2 (`chart-map-countries.ts`).
 * The 1:50m scale keeps the small countries a deck names - Singapore, Malta,
 * Bahrain, Luxembourg - which the 1:110m one drops; it is loaded with this
 * module only, in the drawing's own lazy chunk.
 *
 * Projection: Natural Earth for the world, Antarctica left out unless the map
 * names it; Lambert azimuthal equal-area on 10E 52N for Europe (the EEA's
 * LAEA Europe); Natural Earth recentred on the Americas and on Asia-Pacific,
 * so neither is cut by the antimeridian. Each region is a box of longitude
 * and latitude fitted into the canvas; what else the canvas holds around it
 * is drawn too, clipped at its edge.
 *
 * Values: each country with one takes its place on the ramp
 * (`mapRampColor`), or its class's flat colour on a classed map
 * (`mapClasses`). A country drawn under `MARKER_BELOW` px across is a
 * marker in its colour too, so it can be seen and hovered - unless the block
 * says `markers: false`, which leaves it its fill alone, however few pixels
 * that is; a country the atlas has no shape for (`UNSHAPED_COUNTRY_POINTS`)
 * is always one, or its value would be drawn nowhere, or in the colour of the
 * country the atlas draws it inside. With `show_values`, a value is written
 * inside its country when its box fits wholly inside the country's largest
 * outline - at its centroid, else at the nearest point that holds it - at the
 * largest size of the chart's text scale that fits, and never on a marker;
 * the rest are read on hover. Values
 * and markers stay inside the region: a country with a value beyond it - off
 * the canvas, or drawn at its edge in its colour - is counted (`outside`).
 */
import { geoAzimuthalEqualArea, geoNaturalEarth1, geoPath, type GeoProjection } from "d3-geo";
import type { Feature, FeatureCollection, Geometry, MultiPoint } from "geojson";
import { feature } from "topojson-client";
import type { GeometryCollection, Topology } from "topojson-specification";
import countries50m from "world-atlas/countries-50m.json";

import type { DeckChartBlock, DeckMapRegion } from "@/types";

import {
  chartValueWriter,
  MAP_EMPTY_FILL,
  mapClasses,
  mapClassOf,
  mapLegendStyle,
  mapMarkersShown,
  mapRampColor,
  mapRampDomain,
  mapRampPosition,
  mapValueSurface,
  seriesColors,
  valuesAsked,
  writtenValues,
} from "./chart-format";
import { chartTextScale, EDGE_PAD, INK_EM, labelWidth } from "./chart-layout";
import { ISO_3166_NUMERIC, UNSHAPED_COUNTRY_POINTS } from "./chart-map-countries";

/** A country drawn under this many canvas px across is marked as well, so it can be seen and hovered. */
const MARKER_BELOW = 10;
/** The radius of a country's marker (canvas px). */
export const MARKER_RADIUS = 7;
/** The room the map keeps from the canvas' edges (canvas px). */
const MAP_PAD = 4;
/** The clear room a value keeps around it inside its country (canvas px, each side). */
const LABEL_PAD_X = 3;
const LABEL_PAD_Y = 2;
/**
 * The most points tried along each side of a country's largest outline for a
 * value that does not fit at its centroid, and the least step between two
 * (canvas px): Great Britain on a map of Europe holds a value at three points
 * of a 3px grid, which a coarser one steps over.
 */
const LABEL_GRID = 24;
const LABEL_GRID_STEP = 2;
/** The ISO 3166-1 code of Antarctica, which a world map leaves out unless it names it. */
const ANTARCTICA = "AQ";

interface RegionView {
  projection: () => GeoProjection;
  /** [west, south, east, north], in degrees: the box fitted into the canvas. */
  box: readonly [number, number, number, number];
}

/** How each region is drawn (see the module comment). */
export const REGION_VIEWS: Readonly<Record<DeckMapRegion, RegionView>> = {
  world: { projection: () => geoNaturalEarth1(), box: [-179.9, -56, 179.9, 84] },
  europe: { projection: () => geoAzimuthalEqualArea().rotate([-10, -52]), box: [-25, 34, 45, 72] },
  americas: { projection: () => geoNaturalEarth1().rotate([100, 0]), box: [-170, -56, -30, 75] },
  apac: { projection: () => geoNaturalEarth1().rotate([-125, 0]), box: [60, -48, 190, 55] },
};

/** The region a block draws: its own, `world` when unset. */
function mapRegion(block: Pick<DeckChartBlock, "region">): DeckMapRegion {
  return block.region && block.region in REGION_VIEWS ? block.region : "world";
}

type Point = readonly [number, number];
type Ring = Point[];
interface Box {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

/** One country of the atlas as the canvas draws it. */
interface MapShape {
  /** The atlas' own key: its numeric code, or its name for a shape ISO does not list. */
  key: string;
  /** Its ISO 3166-1 alpha-2 code; `null` for a shape no code names (Kosovo, Somaliland). */
  code: string | null;
  /** The atlas' English name: what a shape no code names is called. */
  name: string;
  /** The SVG path of its outlines, clipped to the canvas. */
  d: string;
  /** The block category that gives its value; `null` for a country without one. */
  category: number | null;
  fill: string;
}

/** A dot over a country too small to see, or with no shape of its own, in its colour. */
interface MapMarker {
  code: string;
  category: number;
  x: number;
  y: number;
  r: number;
  fill: string;
}

/** A value written inside its country. */
interface MapLabel {
  category: number;
  text: string;
  x: number;
  y: number;
  size: number;
  /** The country's fill, which the value's ink is read from. */
  fill: string;
}

export interface MapLayout {
  region: DeckMapRegion;
  shapes: MapShape[];
  markers: MapMarker[];
  labels: MapLabel[];
  domain: { min: number; max: number };
  /** The categories whose country lies beyond the region: off the canvas, or drawn at its edge. */
  outside: number[];
}

/** One country of the atlas: every feature its code names (Australia and its Ashmore and Cartier Islands). */
interface AtlasCountry {
  key: string;
  code: string | null;
  name: string;
  features: Feature<Geometry, { name: string }>[];
}

const ALPHA2_BY_NUMERIC = new Map(Object.entries(ISO_3166_NUMERIC).map(([alpha2, numeric]) => [numeric, alpha2]));

let atlas: AtlasCountry[] | null = null;

/** Every country of the atlas, decoded once, one entry per code (per name for a shape no code names). */
export function atlasCountries(): readonly AtlasCountry[] {
  if (!atlas) {
    const topology = countries50m as unknown as Topology<{ countries: GeometryCollection<{ name: string }> }>;
    const collection = feature(topology, topology.objects.countries) as FeatureCollection<Geometry, { name: string }>;
    const byKey = new Map<string, AtlasCountry>();
    for (const country of collection.features) {
      const id = country.id === undefined ? null : String(country.id);
      const key = id ?? country.properties.name;
      const entry = byKey.get(key);
      if (entry) entry.features.push(country);
      else {
        byKey.set(key, {
          key,
          code: id ? (ALPHA2_BY_NUMERIC.get(id) ?? null) : null,
          name: country.properties.name,
          features: [country],
        });
      }
    }
    atlas = [...byKey.values()];
  }
  return atlas;
}

/** The region's box as a run of points along its edges: a projection is fitted to what it covers. */
function boxOutline([west, south, east, north]: RegionView["box"]): MultiPoint {
  const steps = 36;
  const coordinates: [number, number][] = [];
  for (let step = 0; step <= steps; step += 1) {
    const lon = west + ((east - west) * step) / steps;
    const lat = south + ((north - south) * step) / steps;
    coordinates.push([lon, south], [lon, north], [west, lat], [east, lat]);
  }
  return { type: "MultiPoint", coordinates };
}

/** The box a region is fitted to: a world map that names Antarctica reaches the pole. */
function regionBox(region: DeckMapRegion, withAntarctica: boolean): RegionView["box"] {
  const { box } = REGION_VIEWS[region];
  return region === "world" && withAntarctica ? [box[0], -90, box[2], box[3]] : box;
}

/** The projection of a region fitted into a canvas, its geometry clipped at the canvas' edge. */
export function mapProjection(region: DeckMapRegion, width: number, height: number, withAntarctica = false): GeoProjection {
  const projection = REGION_VIEWS[region].projection();
  const right = Math.max(MAP_PAD + 1, width - MAP_PAD);
  const bottom = Math.max(MAP_PAD + 1, height - MAP_PAD);
  projection.fitExtent(
    [
      [MAP_PAD, MAP_PAD],
      [right, bottom],
    ],
    boxOutline(regionBox(region, withAntarctica)),
  );
  return projection.clipExtent([
    [0, 0],
    [width, height],
  ]);
}

/**
 * Where the region itself lies on the canvas: the box of its edges as drawn.
 * A wide canvas shows more than the region around it - the Americas' edge of
 * a map of Europe - and a value is written, or a country marked, only inside.
 */
function regionFrame(projection: GeoProjection, region: DeckMapRegion, withAntarctica: boolean): Box {
  const points = boxOutline(regionBox(region, withAntarctica))
    .coordinates.map((point) => projection([point[0], point[1]]))
    .filter((point): point is [number, number] => point !== null);
  return boundsOf([points]);
}

const overlaps = (a: Box, b: Box): boolean => a.x0 <= b.x1 && b.x0 <= a.x1 && a.y0 <= b.y1 && b.y0 <= a.y1;

/** What a projection draws of one country on the canvas. */
interface Projected {
  /** Its outlines, in canvas px, each point at least `SIMPLIFY_PX` from the one before it. */
  rings: Ring[];
  /** The box of every point it reaches before that; `null` when the canvas holds none. */
  extent: Box | null;
}

/**
 * The least distance between two points of a drawn outline (canvas px), and
 * the least area an outline is drawn at (canvas px squared): the 1:50m
 * outlines carry a point every few km and thousands of islets, many to a
 * pixel at world scale, which the canvas could not show - the path would
 * weigh a megabyte. A country left with no outline is still marked.
 */
const SIMPLIFY_PX = 0.75;
const MIN_RING_AREA = 1;

/** The outlines a projection draws of a country, as the path streams them, decimated to `SIMPLIFY_PX`. */
function projectCountry(projection: GeoProjection, country: AtlasCountry): Projected {
  const rings: Ring[] = [];
  const extent = { x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity };
  let ring: Ring | null = null;
  const reach = (x: number, y: number) => {
    extent.x0 = Math.min(extent.x0, x);
    extent.y0 = Math.min(extent.y0, y);
    extent.x1 = Math.max(extent.x1, x);
    extent.y1 = Math.max(extent.y1, y);
  };
  const context = {
    beginPath() {},
    moveTo(x: number, y: number) {
      reach(x, y);
      ring = [[x, y]];
      rings.push(ring);
    },
    lineTo(x: number, y: number) {
      reach(x, y);
      if (!ring) return;
      const [px, py] = ring[ring.length - 1];
      if (Math.abs(x - px) >= SIMPLIFY_PX || Math.abs(y - py) >= SIMPLIFY_PX) ring.push([x, y]);
    },
    closePath() {
      ring = null;
    },
    arc() {},
  };
  const path = geoPath(projection, context);
  for (const part of country.features) path(part);
  return {
    rings: rings.filter((outline) => outline.length > 2 && Math.abs(signedArea(outline)) >= MIN_RING_AREA),
    extent: Number.isFinite(extent.x0) ? extent : null,
  };
}

const fixed = (value: number): string => String(Math.round(value * 10) / 10);

/** The SVG path of a run of outlines. */
function pathOf(rings: readonly Ring[]): string {
  return rings.map((ring) => `M${ring.map(([x, y]) => `${fixed(x)},${fixed(y)}`).join("L")}Z`).join("");
}

function signedArea(ring: Ring): number {
  let area = 0;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
    area += ring[j][0] * ring[i][1] - ring[i][0] * ring[j][1];
  }
  return area / 2;
}

/** A country's largest outline: its mainland, never an island or a hole. */
function largestRing(rings: readonly Ring[]): Ring {
  return rings.reduce((best, ring) => (Math.abs(signedArea(ring)) > Math.abs(signedArea(best)) ? ring : best));
}

function centroid(ring: Ring): Point {
  const area = signedArea(ring);
  if (Math.abs(area) < 1e-9) {
    const sum = ring.reduce(([sx, sy], [x, y]) => [sx + x, sy + y] as const, [0, 0] as const);
    return [sum[0] / ring.length, sum[1] / ring.length];
  }
  let cx = 0;
  let cy = 0;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
    const cross = ring[j][0] * ring[i][1] - ring[i][0] * ring[j][1];
    cx += (ring[j][0] + ring[i][0]) * cross;
    cy += (ring[j][1] + ring[i][1]) * cross;
  }
  return [cx / (6 * area), cy / (6 * area)];
}

function boundsOf(rings: readonly Ring[]): Box {
  const box = { x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity };
  for (const ring of rings) {
    for (const [x, y] of ring) {
      box.x0 = Math.min(box.x0, x);
      box.y0 = Math.min(box.y0, y);
      box.x1 = Math.max(box.x1, x);
      box.y1 = Math.max(box.y1, y);
    }
  }
  return box;
}

/** Whether a point is inside the outlines (even-odd: a hole - Lesotho in South Africa - is outside). */
function insideRings([px, py]: Point, rings: readonly Ring[]): boolean {
  let inside = false;
  for (const ring of rings) {
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
      const [xi, yi] = ring[i];
      const [xj, yj] = ring[j];
      if (yi > py !== yj > py && px < ((xj - xi) * (py - yi)) / (yj - yi) + xi) inside = !inside;
    }
  }
  return inside;
}

/** Whether the segment a-b meets the box (Liang-Barsky). */
function segmentMeetsBox([ax, ay]: Point, [bx, by]: Point, box: Box): boolean {
  const dx = bx - ax;
  const dy = by - ay;
  let low = 0;
  let high = 1;
  for (const [p, q] of [
    [-dx, ax - box.x0],
    [dx, box.x1 - ax],
    [-dy, ay - box.y0],
    [dy, box.y1 - ay],
  ]) {
    if (p === 0) {
      if (q < 0) return false;
      continue;
    }
    const t = q / p;
    if (p < 0) {
      if (t > high) return false;
      low = Math.max(low, t);
    } else {
      if (t < low) return false;
      high = Math.min(high, t);
    }
  }
  return true;
}

/** Whether a box lies wholly inside the outlines: its centre inside, and no outline crossing it. */
function boxInside(box: Box, rings: readonly Ring[]): boolean {
  if (!insideRings([(box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2], rings)) return false;
  return !rings.some((ring) => ring.some((point, index) => segmentMeetsBox(ring[index === 0 ? ring.length - 1 : index - 1], point, box)));
}

/**
 * Where a value of `width` x `height` canvas px is written inside a country,
 * or `null`: at the centroid of its largest outline when the box fits there,
 * else at the point of a grid over that outline that holds it nearest the
 * centroid. Never out of `room`: the region, inside the canvas' edge.
 */
function labelPlace(rings: readonly Ring[], width: number, height: number, room: Box): Point | null {
  const main = largestRing(rings);
  const outline = boundsOf([main]);
  if (outline.x1 - outline.x0 < width || outline.y1 - outline.y0 < height) return null;
  const fits = ([x, y]: Point): boolean => {
    const box = { x0: x - width / 2, y0: y - height / 2, x1: x + width / 2, y1: y + height / 2 };
    if (box.x0 < room.x0 || box.y0 < room.y0 || box.x1 > room.x1 || box.y1 > room.y1) return false;
    return boxInside(box, rings);
  };
  const center = centroid(main);
  if (fits(center)) return center;
  const stepX = Math.max(LABEL_GRID_STEP, (outline.x1 - outline.x0) / LABEL_GRID);
  const stepY = Math.max(LABEL_GRID_STEP, (outline.y1 - outline.y0) / LABEL_GRID);
  const grid: Point[] = [];
  for (let y = outline.y0 + height / 2; y <= outline.y1 - height / 2; y += stepY) {
    for (let x = outline.x0 + width / 2; x <= outline.x1 - width / 2; x += stepX) grid.push([x, y]);
  }
  const distance = ([x, y]: Point) => (x - center[0]) ** 2 + (y - center[1]) ** 2;
  return grid.sort((a, b) => distance(a) - distance(b)).find(fits) ?? null;
}

const insideCanvas = ([x, y]: Point, width: number, height: number): boolean => x >= 0 && y >= 0 && x <= width && y <= height;

/** A country with a value, drawn large enough to hold one. */
interface Valued {
  category: number;
  text: string;
  rings: Ring[];
  fill: string;
}

/**
 * The values written inside their countries, each at the largest of the text
 * scale's sizes its country holds - set smaller, down to the scale's
 * smallest, before it is left to the hover, as an axis chart's values are.
 * A map has no axis to read them against: a large country writes
 * its value large, as a map labels it, and one small country never shrinks
 * the others' values.
 */
function placeLabels(valued: readonly Valued[], sizes: readonly number[], room: Box): MapLabel[] {
  const labels: MapLabel[] = [];
  for (const entry of valued) {
    for (const size of sizes) {
      const place = labelPlace(entry.rings, labelWidth(entry.text, size) + 2 * LABEL_PAD_X, size * INK_EM + 2 * LABEL_PAD_Y, room);
      if (place) {
        labels.push({ category: entry.category, text: entry.text, x: place[0], y: place[1], size, fill: entry.fill });
        break;
      }
    }
  }
  return labels;
}

/** A country as another drawing colours it itself: its outlines, and where a marker goes when it is too small to see. */
export interface AtlasPath {
  key: string;
  code: string | null;
  name: string;
  /** Its outlines as one SVG path, decimated like the deck's map; empty when none is large enough to draw. */
  d: string;
  /** Its mainland's centre when it is drawn under `MARKER_BELOW` px across, else `null`. */
  speck: Point | null;
}

/**
 * Every country of the atlas as `projection` draws it, for a map that colours
 * its countries itself (a dashboard's choropleth): the same outlines as a
 * deck's map, decimated the same way. Antarctica only with `withAntarctica`.
 */
export function atlasPaths(projection: GeoProjection, withAntarctica = false): AtlasPath[] {
  const paths: AtlasPath[] = [];
  for (const country of atlasCountries()) {
    if (country.code === ANTARCTICA && !withAntarctica) continue;
    const { rings, extent } = projectCountry(projection, country);
    if (!extent) continue;
    const main = rings.length ? largestRing(rings) : null;
    const box = main ? boundsOf([main]) : extent;
    const small = Math.max(box.x1 - box.x0, box.y1 - box.y0) < MARKER_BELOW;
    const speck: Point | null = small ? (main ? centroid(main) : [(box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2]) : null;
    paths.push({ key: country.key, code: country.code, name: country.name, d: rings.length ? pathOf(rings) : "", speck });
  }
  return paths;
}

/** The whole map of a block on a canvas of `width` x `height` (see the module comment). */
export function mapChartLayout(block: DeckChartBlock, width: number, height: number, locale: string): MapLayout {
  const region = mapRegion(block);
  const values = block.series[0]?.values ?? [];
  const byCode = new Map(block.categories.map((code, index) => [code.toUpperCase(), index]));
  const withAntarctica = byCode.has(ANTARCTICA);
  const projection = mapProjection(region, width, height, withAntarctica);
  const domain = mapRampDomain(block);
  const end = seriesColors(block.series)[0];
  const classes = mapLegendStyle(block) === "classes" ? mapClasses(block, locale) : null;
  const fillOf = (category: number) => {
    const value = values[category] ?? 0;
    return (classes && mapClassOf(classes, value)?.color) || mapRampColor(mapRampPosition(value, domain), end);
  };
  const dotted = mapMarkersShown(block);
  const write = chartValueWriter(block, writtenValues(block), locale, mapValueSurface(block));
  const frame = regionFrame(projection, region, withAntarctica);
  // A value is written inside the region, never nearer the canvas' edge than `EDGE_PAD`.
  const room = {
    x0: Math.max(EDGE_PAD, frame.x0),
    y0: Math.max(EDGE_PAD, frame.y0),
    x1: Math.min(width - EDGE_PAD, frame.x1),
    y1: Math.min(height - EDGE_PAD, frame.y1),
  };

  const shapes: MapShape[] = [];
  const markers: MapMarker[] = [];
  const valued: Valued[] = [];
  const outside: number[] = [];
  const drawn = new Set<number>();
  for (const country of atlasCountries()) {
    if (country.code === ANTARCTICA && !withAntarctica) continue;
    const { rings, extent } = projectCountry(projection, country);
    if (!extent) continue;
    const category = country.code ? (byCode.get(country.code) ?? null) : null;
    const fill = category === null ? MAP_EMPTY_FILL : fillOf(category);
    if (rings.length) shapes.push({ key: country.key, code: country.code, name: country.name, d: pathOf(rings), category, fill });
    if (category === null || country.code === null) continue;
    // Read off the mainland: an archipelago spread over an ocean is still a speck per island.
    const main = rings.length ? largestRing(rings) : null;
    const box = main ? boundsOf([main]) : extent;
    // Drawn at the edge of another region's map, in its colour: counted as outside it.
    if (!overlaps(box, frame)) continue;
    drawn.add(category);
    if (Math.max(box.x1 - box.x0, box.y1 - box.y0) < MARKER_BELOW) {
      // `markers: false` leaves it its fill alone, however few pixels that is (Singapore at world scale: none).
      if (!dotted) continue;
      const [x, y] = main ? centroid(main) : [(box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2];
      markers.push({ code: country.code, category, x, y, r: MARKER_RADIUS, fill });
      continue;
    }
    valued.push({ category, text: write(values[category] ?? 0), rings, fill });
  }
  const labels = valuesAsked(block) ? placeLabels(valued, chartTextScale(block).values, room) : [];
  block.categories.forEach((raw, category) => {
    if (drawn.has(category)) return;
    const code = raw.toUpperCase();
    const point = UNSHAPED_COUNTRY_POINTS[code];
    const projected = point ? projection([point[0], point[1]]) : null;
    if (projected && insideCanvas(projected, width, height) && overlaps(boundsOf([[projected]]), frame)) {
      markers.push({ code, category, x: projected[0], y: projected[1], r: MARKER_RADIUS, fill: fillOf(category) });
    } else {
      outside.push(category);
    }
  });
  return { region, shapes, markers, labels, domain, outside };
}
