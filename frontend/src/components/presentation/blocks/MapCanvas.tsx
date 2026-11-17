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
 * The drawing of a `map` chart: the countries of the world as one
 * SVG at one explicit size, in the deck's tokens. Loaded lazily by
 * `chart.tsx` in a chunk of its own - with the 1:50m atlas - so a deck with
 * no map never downloads it and a map never downloads Recharts.
 *
 * Every colour is a deck token: a country with a value is drawn on its
 * series' ramp (`mapRampColor`, over `--deck-map-paper`) - its class's flat
 * step of it on a classed map - one without in
 * `--deck-map-empty`, the borders in the paper, so the figure follows the
 * template and the theme live and the export's screenshot carries them. Which
 * country is drawn where, which is marked and which writes its value is
 * decided in `chart-map.ts`; this file draws what that decided, and reads the
 * hover: the country under the pointer, its name in the reader's language and
 * its value, in a tooltip drawn in canvas px inside the scaled stage.
 *
 * The figure is drawn whole - nothing grows in - so it is complete two frames
 * after it is painted (`createSettleClock`); on a slide entry the stylesheet
 * fades it in. It publishes in `.deck-chart-facts` what the layout check and
 * the tests read: the region, how many values it wrote inside their country,
 * how many countries it marked, those the region leaves out, and the ramp's
 * ends. A country too small for its value is not a finding - the map writes
 * it on hover - one beyond the region is ("values hidden on chart N").
 */
import { useEffect, useMemo, useState, type CSSProperties, type PointerEvent } from "react";

import type { DeckChartBlock } from "@/types";

import { chartValueWriter, writtenValues } from "./chart-format";
import type { HiddenValuesReason } from "./chart-layout";
import { mapChartLayout, type MapLayout } from "./chart-map";
import { createSettleClock } from "./chart-settle";

interface MapCanvasProps {
  block: DeckChartBlock;
  width: number;
  height: number;
  locale: string;
  /** Called once the figure is painted, so the host can drop `data-deck-pending`. */
  onSettled: () => void;
  /** What the hover says of a country without a value, in the app's language. */
  noDataLabel: string;
}

/** What the pointer is over: a country's shape or marker, at a canvas point. */
interface Hover {
  shape: number | null;
  category: number | null;
  name: string;
  /** The country's own colour: its place on the ramp, or the empty fill. */
  fill: string;
  x: number;
  y: number;
}

const REGION_NAMES = new Map<string, Intl.DisplayNames | null>();

function regionNames(locale: string): Intl.DisplayNames | null {
  try {
    return new Intl.DisplayNames([locale, "en"], { type: "region" });
  } catch {
    return null;
  }
}

/** A country's name in the reader's language (`FR` -> `Frankreich`), the atlas' English one where ICU has none. */
function countryNamer(locale: string): (code: string | null, fallback: string) => string {
  if (!REGION_NAMES.has(locale)) REGION_NAMES.set(locale, regionNames(locale));
  const names = REGION_NAMES.get(locale) ?? null;
  return (code, fallback) => {
    if (!code || !names) return fallback;
    try {
      return names.of(code) ?? fallback;
    } catch {
      return fallback;
    }
  };
}

const round2 = (value: number): number => Math.round(value * 100) / 100;

/**
 * A country is written on the map when it is drawn inside the region - its
 * value inside it, or on hover - so the layout check counts as hidden only
 * the countries the region leaves out (`data-values-reason="region"`, named
 * by code in `data-values-countries`).
 */
const BEYOND_THE_REGION: HiddenValuesReason = "region";

function MapFacts({ block, layout }: { block: DeckChartBlock; layout: MapLayout }) {
  const asked = block.categories.length;
  const outside = layout.outside.map((category) => block.categories[category]);
  return (
    <span
      className="deck-chart-facts"
      hidden
      data-region={layout.region}
      data-values-asked={asked}
      data-values-written={asked - outside.length}
      {...(outside.length
        ? { "data-values-reason": BEYOND_THE_REGION, "data-values-countries": outside.join(",") }
        : {})}
      data-values-labelled={layout.labels.length}
      data-values-marked={layout.markers.length}
      data-values-outside={outside.length}
      data-ramp-min={layout.domain.min}
      data-ramp-max={layout.domain.max}
    />
  );
}

export default function MapCanvas({ block, width, height, locale, onSettled, noDataLabel }: MapCanvasProps) {
  const clock = useMemo(() => createSettleClock(onSettled), [onSettled]);
  useEffect(() => {
    clock.start(false);
    return () => clock.stop();
  }, [clock]);

  const layout = useMemo(() => mapChartLayout(block, width, height, locale), [block, width, height, locale]);
  const nameOf = useMemo(() => countryNamer(locale), [locale]);
  const write = useMemo(() => chartValueWriter(block, writtenValues(block), locale), [block, locale]);
  const [hover, setHover] = useState<Hover | null>(null);
  const series = block.series[0];
  const values = series?.values ?? [];

  const summary = useMemo(() => {
    const read = block.series[0]?.values ?? [];
    const countries = block.categories.map((code, index) => `${nameOf(code, code)} ${write(read[index] ?? 0)}`);
    return `${block.series[0]?.name ?? ""}: ${countries.join(", ")}`;
  }, [block, nameOf, write]);

  const onPointerMove = (event: PointerEvent<SVGSVGElement>) => {
    const target = (event.target as Element).closest<SVGElement>("[data-shape], [data-marker]");
    if (!target) {
      setHover(null);
      return;
    }
    const rect = event.currentTarget.getBoundingClientRect();
    const scale = rect.width > 0 ? rect.width / width : 1;
    const x = (event.clientX - rect.left) / scale;
    const y = (event.clientY - rect.top) / scale;
    if (target.dataset.marker !== undefined) {
      const marker = layout.markers[Number(target.dataset.marker)];
      if (marker) {
        setHover({ shape: null, category: marker.category, name: nameOf(marker.code, marker.code), fill: marker.fill, x, y });
      }
      return;
    }
    const index = Number(target.dataset.shape);
    const shape = layout.shapes[index];
    if (shape) {
      setHover({ shape: index, category: shape.category, name: nameOf(shape.code, shape.name), fill: shape.fill, x, y });
    }
  };

  const hovered = hover?.shape != null ? layout.shapes[hover.shape] : null;

  return (
    <>
      <svg
        className="deck-chart-map"
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={summary}
        onPointerMove={onPointerMove}
        onPointerLeave={() => setHover(null)}
      >
        <g className="deck-chart-countries">
          {layout.shapes.map((shape, index) => (
            <path
              key={shape.key}
              className="deck-chart-country"
              d={shape.d}
              fill={shape.fill}
              data-shape={index}
              data-code={shape.code ?? undefined}
              data-category={shape.category ?? undefined}
            />
          ))}
        </g>
        {hovered ? <path className="deck-chart-country-hover" d={hovered.d} aria-hidden="true" /> : null}
        {layout.markers.map((marker, index) => (
          <circle
            key={`${marker.code}:${marker.category}`}
            className="deck-chart-marker"
            cx={round2(marker.x)}
            cy={round2(marker.y)}
            r={marker.r}
            fill={marker.fill}
            data-marker={index}
            data-code={marker.code}
            data-category={marker.category}
          />
        ))}
        {layout.labels.map((label) => (
          <text
            key={label.category}
            className="deck-chart-value"
            x={round2(label.x)}
            y={round2(label.y)}
            textAnchor="middle"
            dominantBaseline="central"
            style={{ fontSize: label.size, "--deck-segment": label.fill } as CSSProperties}
            data-role="country"
            data-series={0}
            data-category={label.category}
          >
            {label.text}
          </text>
        ))}
      </svg>
      {hover ? (
        <div
          className="deck-chart-tip"
          style={{ left: hover.x, top: hover.y }}
          data-side={hover.x > width / 2 ? "left" : "right"}
          data-edge={hover.y > height / 2 ? "above" : "below"}
        >
          <div className="deck-chart-tooltip" role="status">
            <div className="deck-chart-tooltip-title">{hover.name}</div>
            <ul className="deck-chart-tooltip-rows">
              <li className="deck-chart-tooltip-row">
                <span
                  className="deck-chart-swatch"
                  style={{ "--deck-tip-swatch": hover.fill } as CSSProperties}
                  aria-hidden="true"
                />
                <span className="deck-chart-tooltip-name">{series?.name ?? ""}</span>
                <span className="deck-chart-tooltip-value">
                  {hover.category === null ? noDataLabel : write(values[hover.category] ?? 0)}
                </span>
              </li>
            </ul>
          </div>
        </div>
      ) : null}
      <MapFacts block={block} layout={layout} />
    </>
  );
}
