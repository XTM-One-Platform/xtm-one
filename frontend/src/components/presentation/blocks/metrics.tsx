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

/** KPI tiles: the `metric` block and the `metrics_grid` block. */
import { useRef } from "react";

import type { DeckMetricBlock, DeckMetricItem, DeckMetricsGridBlock, DeckMetricVariant } from "@/types";

import { DeckIcon } from "../DeckIcon";
import { useBlockEditAttrs } from "../edit-context";
import { InlineMarkup, PlainText } from "../inline-markup";
import { SPREAD_ROW_MAX_EXTRA_PX, useSpreadCap } from "../spread";
import { chartValueWriter } from "./chart-format";
import { useChartLocale } from "./chart-locale";
import { tileAdornments } from "./metric-look";
import { IconText } from "./shared";

const TREND_ICON = { up: "trending-up", down: "trending-down", flat: "minus" } as const;

/** Drawing box of a sparkline, in its own viewBox units (sized by CSS). */
const SPARK_W = 120;
const SPARK_H = 40;
const SPARK_PAD = 4;

/**
 * The trend behind a KPI: a small line over the tile's `sparkline`
 * points, oldest first, drawn beside the value in the tile's tone with a
 * light fill under it. A shape, not a plot - no axis, no labels, so it adds
 * nothing to the layout audit's glyph count - but not a decoration either: on
 * a tile with no `delta` it is the only trend cue, so assistive technology
 * gets its endpoints (`4,100 -> 6,250`, in the reader's number locale) as the
 * image's name.
 */
function Sparkline({ values }: { values: number[] }) {
  const locale = useChartLocale();
  if (values.length < 2) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const step = (SPARK_W - SPARK_PAD * 2) / (values.length - 1);
  const points = values.map((value, index) => {
    const x = SPARK_PAD + index * step;
    const y = SPARK_PAD + (1 - (value - min) / span) * (SPARK_H - SPARK_PAD * 2);
    return [Math.round(x * 100) / 100, Math.round(y * 100) / 100] as const;
  });
  const line = points.map(([x, y]) => `${x},${y}`).join(" ");
  const last = points[points.length - 1];
  const area = `${SPARK_PAD},${SPARK_H - SPARK_PAD} ${line} ${last[0]},${SPARK_H - SPARK_PAD}`;
  const ends = [values[0], values[values.length - 1]];
  const write = chartValueWriter({}, ends, locale);
  const endpoints = `${write(ends[0])} \u2192 ${write(ends[1])}`;
  return (
    <svg
      className="deck-sparkline"
      viewBox={`0 0 ${SPARK_W} ${SPARK_H}`}
      role="img"
      aria-label={endpoints}
      focusable="false"
    >
      <polygon className="deck-sparkline-area" points={area} />
      <polyline className="deck-sparkline-line" points={line} />
      <circle className="deck-sparkline-dot" cx={last[0]} cy={last[1]} r={3.5} />
    </svg>
  );
}

/**
 * One KPI tile. `field` is where the metric lives relative to the enclosing
 * scope: nothing for the `metric` block (its fields are its own), `items.N`
 * in a grid, `tiles.N` on a dashboard slide, `figures.N` under a quote.
 *
 * `variant` / `align`: `plain` drops the card (surface, border,
 * accent bar) and draws the value and the label only (`tileAdornments`), and
 * `center` centres the tile's content. Neither attribute is written at its
 * default, so a card tile keeps the DOM it always had.
 */
export function MetricTile({
  metric,
  field,
  editAttrs,
  variant,
  align,
}: {
  metric: DeckMetricItem;
  field?: string;
  editAttrs?: Record<string, string | undefined>;
  variant?: DeckMetricVariant;
  align?: "start" | "center";
}) {
  const trend = metric.trend ?? (metric.delta ? "flat" : undefined);
  const at = (name: string) => (field ? `${field}.${name}` : name);
  const drawn = tileAdornments(metric, variant);
  return (
    <div
      className="deck-metric"
      data-tone={metric.tone ?? "accent"}
      data-variant={variant === "plain" ? "plain" : undefined}
      data-align={align === "center" ? "center" : undefined}
      {...editAttrs}
    >
      {drawn.icon ? (
        <span className="deck-metric-icon">
          <DeckIcon name={drawn.icon} />
        </span>
      ) : null}
      <div className="deck-metric-value">
        <PlainText as="span" value={metric.value} field={at("value")} />
        {drawn.sparkline ? <Sparkline values={drawn.sparkline} /> : null}
      </div>
      <InlineMarkup as="div" className="deck-metric-label" value={metric.label} field={at("label")} />
      {drawn.sublabel ? (
        <InlineMarkup as="div" className="deck-metric-sublabel" value={drawn.sublabel} field={at("sublabel")} />
      ) : null}
      {drawn.delta ? (
        // The trend arrow and the delta are the shared icon + text row.
        <IconText
          as="div"
          className="deck-metric-delta"
          data-trend={trend}
          data-tone={metric.delta_tone}
          icon={trend ? <DeckIcon name={TREND_ICON[trend]} /> : undefined}
        >
          <PlainText as="span" value={drawn.delta} field={at("delta")} />
        </IconText>
      ) : null}
    </div>
  );
}

export function MetricBlockView({ block }: { block: DeckMetricBlock }) {
  return <MetricTile metric={block} variant={block.variant} align={block.align} editAttrs={useBlockEditAttrs()} />;
}

/**
 * A grid of KPI tiles. Its `size` is held when its slide's body is
 * shrunk to fit (`data-fit-hold`, `fit.ts`), so the same grid reads the same
 * on every slide of a deck.
 */
export function MetricsGridBlockView({ block }: { block: DeckMetricsGridBlock }) {
  const columns = block.columns ?? Math.min(block.items.length, 4);
  // `fill` spreads the rows of tiles up to a readable gap, never to the floor.
  const rootRef = useRef<HTMLDivElement>(null);
  useSpreadCap(rootRef, block.fill, SPREAD_ROW_MAX_EXTRA_PX, block.items);
  return (
    <div
      ref={rootRef}
      className="deck-metrics"
      data-columns={columns}
      data-density={block.density ?? "normal"}
      data-size={block.size ?? "md"}
      data-fill={block.fill ? "true" : undefined}
      data-fit-hold="metrics_grid"
      {...useBlockEditAttrs()}
    >
      {block.items.map((metric, index) => (
        <MetricTile key={index} metric={metric} field={`items.${index}`} variant={block.variant} align={block.align} />
      ))}
    </div>
  );
}
