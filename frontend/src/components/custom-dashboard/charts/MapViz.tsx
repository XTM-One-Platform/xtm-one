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
 * A dashboard's map: the bundled world atlas (Natural Earth 1:50m, the decks'
 * own, `chart-map.ts`) with up to three layers - a choropleth of regions by
 * ISO 3166-1 alpha-2 code, bubbles sized by value, markers - over the region
 * the visualization names (`world`, `europe`, `americas`, `apac`; a region is
 * drawn inside its own box, whatever the cell's shape). Nothing is
 * fetched: no tile server (the CSP allows `'self'` only, and the export
 * browser refuses other origins).
 *
 * A country too small to see that has a value is drawn as a dot of its
 * colour, as on a deck's map; a point off the region is not drawn. Each
 * shape, dot and bubble names itself and its value on hover (`<title>`).
 *
 * Its look may follow a threat map's (`viz/geo.ts` `readMapLook`): regions
 * coloured by their level on the threat scale, thin and outlined, over a
 * contrast basemap; markers drawn as an icon; and a Web Mercator view at a
 * zoom and a centre instead of the whole region.
 */
import { useId, useMemo } from "react";
import { geoMercator } from "d3-geo";

import { atlasPaths, mapProjection, MARKER_RADIUS, REGION_VIEWS } from "@/components/presentation/blocks/chart-map";
import { UNSHAPED_COUNTRY_POINTS } from "@/components/presentation/blocks/chart-map-countries";
import { DeckIcon } from "@/components/presentation/DeckIcon";
import { useColorMode } from "@/hooks/useColorMode";
import { useFormatter } from "@/i18n";
import { formatCompactNumber } from "@/lib/utils";
import type { DeckMapRegion, WidgetGeoData } from "@/types";

import {
  bubbleRadius,
  choroplethClasses,
  classFill,
  CONTRAST_BASEMAP,
  LEVEL_FILL_OPACITY,
  mapLayers,
  mercatorFrame,
  readMapLook,
  regionsByCode,
  viewCenter,
  CLASSES,
} from "../viz/geo";
import { threatColor } from "./palettes";
import { vizColor, VIZ_EMPTY, VIZ_MUTED, VIZ_SURFACE } from "./viz-colors";

const REGIONS: readonly DeckMapRegion[] = ["world", "europe", "americas", "apac"];

/**
 * Where a region's own box lies on the canvas: a wide cell around a map of
 * Europe would otherwise show Alaska in the corner. The world shows all.
 */
function regionFrame(region: DeckMapRegion, project: (point: [number, number]) => [number, number] | null) {
  if (region === "world") return null;
  const [west, south, east, north] = REGION_VIEWS[region].box;
  const points: [number, number][] = [];
  for (let step = 0; step <= 36; step += 1) {
    const lon = west + ((east - west) * step) / 36;
    const lat = south + ((north - south) * step) / 36;
    for (const point of [[lon, south], [lon, north], [west, lat], [east, lat]] as [number, number][]) {
      const at = project(point);
      if (at) points.push(at);
    }
  }
  if (points.length === 0) return null;
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  return { x: Math.min(...xs), y: Math.min(...ys), w: Math.max(...xs) - Math.min(...xs), h: Math.max(...ys) - Math.min(...ys) };
}
const LEGEND_HEIGHT = 22;
const ANTARCTICA = "AQ";

function regionNamer(locale: string): (code: string, fallback: string) => string {
  let names: Intl.DisplayNames | null = null;
  try {
    names = new Intl.DisplayNames([locale, "en"], { type: "region" });
  } catch {
    names = null;
  }
  return (code, fallback) => {
    try {
      return names?.of(code) ?? fallback;
    } catch {
      return fallback;
    }
  };
}

export default function MapViz({
  data,
  options,
  width,
  height,
}: {
  data: WidgetGeoData;
  options: Record<string, unknown>;
  width: number;
  height: number;
}) {
  const { t_i18n, locale } = useFormatter();
  const mode = useColorMode();
  const look = useMemo(() => readMapLook(options), [options]);
  const levelled = look.regionColors === "level";
  const contrast = look.basemap === "contrast" ? CONTRAST_BASEMAP[mode] : null;
  const region = REGIONS.find((r) => r === options.region) ?? "world";
  const layers = mapLayers(options, data);
  const choropleth = layers.includes("choropleth");
  const regions = useMemo(() => regionsByCode(data, true), [data]);
  const withAntarctica = regions.has(ANTARCTICA);
  const mapHeight = Math.max(60, height - (choropleth && !levelled ? LEGEND_HEIGHT : 0));
  const zoom = look.view?.zoom;
  const center = useMemo(() => (look.view ? viewCenter(look.view, data) : null), [look, data]);
  const projection = useMemo(() => {
    if (zoom === undefined) return mapProjection(region, width, mapHeight, withAntarctica);
    // A Web Mercator view of the zoom and centre asked, as a tiled map opens.
    const { scale, translate } = mercatorFrame(zoom, center ?? [0, 0], width, mapHeight);
    return geoMercator()
      .scale(scale)
      .translate(translate)
      .clipExtent([
        [0, 0],
        [width, mapHeight],
      ]);
  }, [region, width, mapHeight, withAntarctica, zoom, center]);
  const countries = useMemo(() => atlasPaths(projection, withAntarctica), [projection, withAntarctica]);
  const frame = useMemo(() => (zoom === undefined ? regionFrame(region, (point) => projection(point)) : null), [region, projection, zoom]);
  // A tiled map's borders thicken from 0.5 px at zoom 1 to 1.5 px at zoom 6.
  const borderWidth = zoom === undefined ? 0.5 : Math.min(1.5, Math.max(0.5, 0.5 + ((zoom - 1) * 1) / 5));
  const clipId = `map-frame-${useId().replace(/[^A-Za-z0-9_-]/g, "")}`;
  const classes = useMemo(() => choroplethClasses(data.regions.map((r) => r.value)), [data]);
  const nameOf = useMemo(() => regionNamer(locale), [locale]);
  const fillOf = (value: number) => classFill(classes.classOf(value));
  const levelOf = (code: string) => threatColor(regions.get(code)?.level);

  const shaped = new Set(countries.map((country) => country.code).filter(Boolean));
  // A code the atlas has no shape for is a dot where the country is.
  const unshaped = choropleth
    ? data.regions.flatMap((r) => {
        const code = r.code.toUpperCase();
        const point = UNSHAPED_COUNTRY_POINTS[code];
        const at = !shaped.has(code) && point ? projection([point[0], point[1]]) : null;
        return at ? [{ region: r, x: at[0], y: at[1] }] : [];
      })
    : [];
  const maxValue = Math.max(0, ...data.points.map((p) => p.value ?? 0));
  const groups = [...new Set(data.points.map((p) => p.group).filter((g): g is string => !!g))];
  const points = data.points.flatMap((point) => {
    const at = projection([point.lon, point.lat]);
    if (!at || at[0] < 0 || at[1] < 0 || at[0] > width || at[1] > mapHeight) return [];
    // Off the region's own box: not on this map.
    if (frame && (at[0] < frame.x || at[0] > frame.x + frame.w || at[1] < frame.y || at[1] > frame.y + frame.h)) return [];
    return [{ point, x: at[0], y: at[1], tone: point.group ? groups.indexOf(point.group) : null }];
  });

  const label = (name: string, value: number | null) => (value === null ? name : `${name}: ${formatCompactNumber(value)}`);

  return (
    <div className="flex h-full flex-col" data-viz="map" data-region={region}>
      <div className="relative" style={{ width, height: mapHeight }}>
      <svg
        width={width}
        height={mapHeight}
        viewBox={`0 0 ${width} ${mapHeight}`}
        role="img"
        aria-label={t_i18n("{count, plural, one {Map of # place} other {Map of # places}}", { count: data.regions.length + data.points.length })}
        className={contrast ? "rounded-md" : undefined}
        data-basemap={look.basemap}
        data-region-colors={look.regionColors}
      >
        {contrast ? <rect width={width} height={mapHeight} style={{ fill: contrast.sea }} data-sea="" /> : null}
        {frame ? (
          <clipPath id={clipId}>
            <rect x={frame.x} y={frame.y} width={frame.w} height={frame.h} rx={4} />
          </clipPath>
        ) : null}
        <g clipPath={frame ? `url(#${clipId})` : undefined}>
          {countries.map((country) => {
            const valued = choropleth && country.code ? regions.get(country.code) : undefined;
            const fill = valued ? (levelled ? levelOf(country.code as string) : fillOf(valued.value)) : (contrast?.land ?? VIZ_EMPTY);
            const name = country.code ? nameOf(country.code, country.name) : country.name;
            // A levelled region is its level's colour, thin over the land, outlined 1 px in that colour.
            const style =
              valued && levelled
                ? { fill, fillOpacity: LEVEL_FILL_OPACITY[mode], stroke: fill, strokeWidth: 1 }
                : { fill, stroke: contrast?.border ?? VIZ_SURFACE, strokeWidth: contrast ? borderWidth : 0.5 };
            return (
              <g key={country.key}>
                {country.d && valued && levelled && contrast ? <path d={country.d} style={{ fill: contrast.land, stroke: "none" }} /> : null}
                {country.d ? (
                  <path
                    d={country.d}
                    style={style}
                    data-code={country.code ?? undefined}
                    data-valued={valued ? "" : undefined}
                    data-level={valued && levelled ? (valued.level ?? 0) : undefined}
                  >
                    <title>{label(name, valued?.value ?? null)}</title>
                  </path>
                ) : null}
                {valued && country.speck ? (
                  <circle cx={country.speck[0]} cy={country.speck[1]} r={MARKER_RADIUS / 1.5} style={{ fill, stroke: VIZ_SURFACE }}>
                    <title>{label(name, valued.value)}</title>
                  </circle>
                ) : null}
              </g>
            );
          })}
        {levelled
          ? countries.map((country) =>
              country.d && country.code && regions.has(country.code) ? (
                // Its outline above its neighbours' borders, as a threat map draws its highlighted countries.
                <path key={`o-${country.key}`} d={country.d} style={{ fill: "none", stroke: levelOf(country.code), strokeWidth: 1 }} data-outline={country.code} />
              ) : null,
            )
          : null}
        {unshaped.map(({ region: r, x, y }) => (
          <circle key={`u-${r.code}`} cx={x} cy={y} r={MARKER_RADIUS / 1.5} style={{ fill: levelled ? levelOf(r.code.toUpperCase()) : fillOf(r.value), stroke: VIZ_SURFACE }}>
            <title>{label(nameOf(r.code.toUpperCase(), r.label), r.value)}</title>
          </circle>
        ))}
        {layers.includes("bubble")
          ? points.map(({ point, x, y, tone }, index) => (
              <circle
                key={`b-${index}`}
                cx={x}
                cy={y}
                r={bubbleRadius(point.value, maxValue)}
                style={{ fill: vizColor(tone), fillOpacity: 0.45, stroke: vizColor(tone), strokeWidth: 1 }}
                data-layer="bubble"
              >
                <title>{label(point.label || "-", point.value)}</title>
              </circle>
            ))
          : null}
        {layers.includes("marker") && !look.markerIcon
          ? points.map(({ point, x, y, tone }, index) => (
              <g key={`m-${index}`} transform={`translate(${x},${y})`} data-layer="marker">
                <path d="M0,0 C-5,-7 -7,-10 -7,-13 A7,7 0 1 1 7,-13 C7,-10 5,-7 0,0Z" style={{ fill: vizColor(tone), stroke: VIZ_SURFACE, strokeWidth: 1 }} />
                <circle cx={0} cy={-13} r={2.5} style={{ fill: VIZ_SURFACE }} />
                <title>{label(point.label || "-", point.value)}</title>
              </g>
            ))
          : null}
        </g>
      </svg>
      {layers.includes("marker") && look.markerIcon
        ? points.map(({ point, x, y }, index) => (
            <span
              key={`i-${index}`}
              className="pointer-events-auto absolute flex items-center justify-center"
              style={{ left: x - look.markerSize / 2, top: y - look.markerSize / 2, width: look.markerSize, height: look.markerSize, color: look.markerColor }}
              title={label(point.label || "-", point.value)}
              data-layer="marker"
              data-marker-icon={look.markerIcon}
            >
              <DeckIcon name={look.markerIcon} size={look.markerSize} />
            </span>
          ))
        : null}
      </div>
      {choropleth && !levelled && classes.breaks.length > 1 ? (
        <div className="flex items-center justify-center gap-2" style={{ height: LEGEND_HEIGHT }} aria-hidden="true">
          <span style={VIZ_MUTED}>{formatCompactNumber(classes.breaks[0])}</span>
          <span className="flex h-2.5 w-40 overflow-hidden rounded-sm">
            {Array.from({ length: CLASSES }, (_, i) => (
              <span key={i} className="h-full flex-1" style={{ background: classFill(i) }} />
            ))}
          </span>
          <span style={VIZ_MUTED}>{formatCompactNumber(classes.breaks[classes.breaks.length - 1])}</span>
        </div>
      ) : null}
    </div>
  );
}
