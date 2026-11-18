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
 * The charts of the evaluation comparison (#12282): a ranking with 95% intervals (forest
 * plot) and a per-entry change between two runs (dumbbell); of one evaluation's Results
 * (#2): quality against cost or time with the Pareto frontier, and how far reruns of one
 * setup spread with which expected items they found every time. Data-viz SVG
 * drawn with design tokens only: colours are CSS variables, text takes the type scale
 * classes.
 */

import { useFormatter } from "@/i18n";

import { EVALUATION_COLORS } from "./compare-colors";

const GRID = { stroke: "var(--border-elevation-subtle)" };
const MUTED = { fill: "var(--text-default-secondary)" };
const INK = { fill: "var(--text-default-primary)" };
const TICKS = [0, 25, 50, 75, 100];

const fmt = (v: number) => v.toFixed(1);
const trunc = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

interface ForestRow {
  id: string;
  label: string;
  value: number;
  low: number;
  high: number;
  best: number;
  worst: number;
  /** Index in `EVALUATION_COLORS`. */
  colorIndex: number;
  /** The platform's default model: named so, in bold. */
  isDefault?: boolean;
  /** Runs pooled into the line, said on hover. */
  runCount?: number;
}

export function ForestPlot({ rows, metricLabel }: { rows: ForestRow[]; metricLabel: string }) {
  const { t_i18n } = useFormatter();
  const W = 760, top = 28, rh = 24, H = top + rows.length * rh + 30, x0 = 330, x1 = 640;
  const X = (v: number) => x0 + ((x1 - x0) * v) / 100;
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="w-full max-w-4xl"
      role="img"
      aria-label={t_i18n("{metric} of each configuration with its 95% interval", { metric: metricLabel })}
    >
      <text x={8} y={16} className="content-caption" style={MUTED}>{t_i18n("Rank")}</text>
      <text x={56} y={16} className="content-caption" style={MUTED}>{t_i18n("Configuration")}</text>
      <text x={x0} y={16} className="content-caption" style={MUTED}>{t_i18n("{metric}, 95% interval", { metric: metricLabel })}</text>
      <text x={W - 8} y={16} className="content-caption" style={MUTED} textAnchor="end">{t_i18n("Value")}</text>
      {TICKS.map((t) => (
        <g key={t}>
          <line x1={X(t)} x2={X(t)} y1={top - 4} y2={H - 26} style={GRID} />
          <text x={X(t)} y={H - 10} className="content-caption" style={MUTED} textAnchor="middle">{t}</text>
        </g>
      ))}
      {rows.map((row, i) => {
        const y = top + i * rh + rh / 2;
        const color = EVALUATION_COLORS[row.colorIndex % EVALUATION_COLORS.length];
        const rank = row.best === row.worst ? String(row.best) : `${row.best}–${row.worst}`;
        const name = row.isDefault ? `${row.label} · ${t_i18n("default")}` : row.label;
        return (
          <g key={row.id}>
            <title>
              {row.runCount != null
                ? t_i18n("{label}: {value} (95% interval {low} to {high}), rank {rank}, {count, plural, one {# run} other {# runs}}", {
                    label: name, value: fmt(row.value), low: fmt(row.low), high: fmt(row.high), rank, count: row.runCount,
                  })
                : t_i18n("{label}: {value} (95% interval {low} to {high}), rank {rank}", {
                    label: name, value: fmt(row.value), low: fmt(row.low), high: fmt(row.high), rank,
                  })}
            </title>
            <text x={8} y={y + 4} className="content-caption tabular-nums" style={INK}>{rank}</text>
            <text x={56} y={y + 4} className={row.isDefault ? "content-caption font-semibold" : "content-caption"} style={INK}>{trunc(name, 44)}</text>
            <line x1={X(row.low)} x2={X(row.high)} y1={y} y2={y} strokeWidth={3} strokeLinecap="round" style={{ stroke: color, opacity: 0.5 }} />
            <circle cx={X(row.value)} cy={y} r={5} style={{ fill: color }} />
            <text x={W - 8} y={y + 4} className="content-caption tabular-nums" style={INK} textAnchor="end">
              {fmt(row.value)} <tspan style={MUTED}>{fmt(row.low)}–{fmt(row.high)}</tspan>
            </text>
          </g>
        );
      })}
    </svg>
  );
}

const ACCENT = "var(--color-accent)";
const FRONT = "var(--color-feedback-success-primary)";
const OFF_FRONT = "var(--text-default-secondary)";
/** Expected items found in every run, in some, never. */
const FOUND_COLORS = {
  every: "var(--color-feedback-success-primary)",
  some: "var(--color-feedback-warning-primary)",
  never: "var(--color-feedback-error-primary)",
} as const;

interface ParetoPoint {
  id: string;
  label: string;
  /** Cost (USD) or seconds per entry. */
  x: number;
  value: number;
  low: number;
  high: number;
  onFront: boolean;
  /** The platform's default model: labelled even off the frontier. */
  isDefault?: boolean;
}

/** Each configuration's figure against its cost or time per entry; the frontier dashed,
 *  from the origin (running nothing costs nothing and finds nothing). */
export function ParetoChart({ points, axis, metricLabel }: { points: ParetoPoint[]; axis: "cost" | "time"; metricLabel: string }) {
  const { t_i18n } = useFormatter();
  const W = 760, H = 380, l = 60, r = 24, t = 16, b = 46;
  const maxX = Math.max(...points.map((p) => p.x), 1e-9) * 1.1;
  const X = (v: number) => l + ((W - l - r) * v) / maxX;
  const Y = (v: number) => t + (H - t - b) * (1 - v / 100);
  const fmtX = (v: number) => (axis === "time" ? `${Math.round(v)} s` : v < 0.1 ? `$${v.toFixed(3)}` : `$${v.toFixed(2)}`);
  const front = points.filter((p) => p.onFront).sort((a, c) => a.x - c.x);
  const axisLabel = axis === "time" ? t_i18n("Agent time per entry (seconds)") : t_i18n("LLM cost per entry (USD)");
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="w-full max-w-4xl"
      role="img"
      aria-label={t_i18n("{metric} against {axis}", { metric: metricLabel, axis: axisLabel })}
    >
      {TICKS.map((v) => (
        <g key={v}>
          <line x1={l} x2={W - r} y1={Y(v)} y2={Y(v)} style={GRID} />
          <text x={l - 8} y={Y(v) + 4} className="content-caption" style={MUTED} textAnchor="end">{v}</text>
        </g>
      ))}
      {[0, 1, 2, 3, 4].map((i) => (
        <text key={i} x={X((maxX * i) / 4)} y={H - b + 18} className="content-caption tabular-nums" style={MUTED} textAnchor="middle">
          {fmtX((maxX * i) / 4)}
        </text>
      ))}
      <text x={(l + W - r) / 2} y={H - 6} className="content-caption" style={MUTED} textAnchor="middle">{axisLabel}</text>
      <text
        x={14}
        y={(t + H - b) / 2}
        className="content-caption"
        style={MUTED}
        textAnchor="middle"
        transform={`rotate(-90 14 ${(t + H - b) / 2})`}
      >
        {metricLabel}
      </text>
      <polyline
        points={[[X(0), Y(0)], ...front.map((p) => [X(p.x), Y(p.value)])].map((q) => q.join(",")).join(" ")}
        fill="none"
        strokeWidth={1.6}
        strokeDasharray="5 4"
        style={{ stroke: FRONT }}
      />
      {points.map((p) => {
        const color = p.onFront ? FRONT : OFF_FRONT;
        // A label past the middle reads leftwards, so the right edge never cuts it.
        const leftward = X(p.x) > (l + W - r) / 2;
        const name = p.isDefault ? `${p.label} · ${t_i18n("default")}` : p.label;
        return (
          <g key={p.id}>
            <title>
              {p.onFront
                ? t_i18n("{label}: {value} (95% interval {low} to {high}) for {x} per entry · on the Pareto frontier", {
                    label: name, value: fmt(p.value), low: fmt(p.low), high: fmt(p.high), x: fmtX(p.x),
                  })
                : t_i18n("{label}: {value} (95% interval {low} to {high}) for {x} per entry", {
                    label: name, value: fmt(p.value), low: fmt(p.low), high: fmt(p.high), x: fmtX(p.x),
                  })}
            </title>
            <line x1={X(p.x)} x2={X(p.x)} y1={Y(p.low)} y2={Y(p.high)} strokeWidth={1.2} style={{ stroke: color, opacity: 0.5 }} />
            <circle cx={X(p.x)} cy={Y(p.value)} r={p.onFront ? 5.5 : 4} strokeWidth={1.5} style={{ fill: color, stroke: "var(--bg-elevation-default)" }} />
            {/* Only the frontier and the default model are named: every label would cover the points. */}
            {(p.onFront || p.isDefault) && (
              <text
                x={X(p.x) + (leftward ? -8 : 8)}
                y={Y(p.value) - 7}
                className={p.onFront ? "content-caption font-semibold" : "content-caption"}
                style={p.onFront ? INK : MUTED}
                textAnchor={leftward ? "end" : "start"}
              >
                {trunc(name, 32)}
              </text>
            )}
          </g>
        );
      })}
      <circle cx={X(0)} cy={Y(0)} r={3} style={{ fill: FRONT }} />
    </svg>
  );
}

interface SpreadRow {
  id: string;
  label: string;
  /** Each run's figure. */
  values: number[];
  mean: number;
  sd: number;
  min: number;
  max: number;
  isDefault?: boolean;
  /** Expected items found by every run, some runs, none; absent when not read. */
  found?: { every: number; some: number; never: number };
}

/**
 * Reruns of one setup: a dot per run, the min–max band and the mean; on the right, when
 * `foundLabel` is given, the expected items found in every run, in some runs, never.
 */
export function ConsistencyChart({ rows, metricLabel, foundLabel }: { rows: SpreadRow[]; metricLabel: string; foundLabel?: string }) {
  const { t_i18n } = useFormatter();
  const right = foundLabel != null;
  const W = 760, top = 28, rh = 30, H = top + rows.length * rh + 30;
  const x0 = right ? 200 : 300, x1 = right ? 500 : 640, b0 = 530, b1 = W - 8;
  const X = (v: number) => x0 + ((x1 - x0) * v) / 100;
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="w-full max-w-4xl"
      role="img"
      aria-label={t_i18n("{metric} of each rerun of a configuration", { metric: metricLabel })}
    >
      <text x={8} y={16} className="content-caption" style={MUTED}>{t_i18n("Configuration · runs")}</text>
      <text x={x0} y={16} className="content-caption" style={MUTED}>{t_i18n("{metric} of each run", { metric: metricLabel })}</text>
      {right ? (
        <text x={b0} y={16} className="content-caption" style={MUTED}>{foundLabel}</text>
      ) : (
        <text x={W - 8} y={16} className="content-caption" style={MUTED} textAnchor="end">{t_i18n("Mean · sd")}</text>
      )}
      {TICKS.map((v) => (
        <g key={v}>
          <line x1={X(v)} x2={X(v)} y1={top - 4} y2={H - 26} style={GRID} />
          <text x={X(v)} y={H - 10} className="content-caption" style={MUTED} textAnchor="middle">{v}</text>
        </g>
      ))}
      {rows.map((row, i) => {
        const y = top + i * rh + rh / 2;
        const found = row.found;
        const total = found ? found.every + found.some + found.never : 0;
        const w = (v: number) => (total ? ((b1 - b0) * v) / total : 0);
        return (
          <g key={row.id}>
            <title>
              {t_i18n("{label}: mean {mean}, sd {sd}, {min} to {max} over {count, plural, one {# run} other {# runs}}", {
                label: row.label, mean: fmt(row.mean), sd: fmt(row.sd), min: fmt(row.min), max: fmt(row.max), count: row.values.length,
              })}
              {found && total > 0
                ? ` ${t_i18n("Found in every run {every}, in some {some}, never {never}.", found)}`
                : ""}
            </title>
            <text x={8} y={y + 4} className={row.isDefault ? "content-caption font-semibold" : "content-caption"} style={INK}>
              {trunc(row.label, right ? 26 : 40)} <tspan style={MUTED}>· {row.values.length}</tspan>
            </text>
            <rect x={X(row.min)} y={y - 7} width={Math.max(1, X(row.max) - X(row.min))} height={14} rx={3} style={{ fill: ACCENT, opacity: 0.14 }} />
            {row.values.map((v, k) => (
              <circle key={k} cx={X(v)} cy={y} r={2.8} style={{ fill: ACCENT, opacity: 0.6 }} />
            ))}
            <line x1={X(row.mean)} x2={X(row.mean)} y1={y - 9} y2={y + 9} strokeWidth={2} style={{ stroke: "var(--text-default-primary)" }} />
            {right ? (
              total > 0 ? (
                <>
                  <rect x={b0} y={y - 6} width={w(found!.every)} height={12} style={{ fill: FOUND_COLORS.every }} />
                  <rect x={b0 + w(found!.every)} y={y - 6} width={w(found!.some)} height={12} style={{ fill: FOUND_COLORS.some }} />
                  <rect x={b0 + w(found!.every) + w(found!.some)} y={y - 6} width={w(found!.never)} height={12} style={{ fill: FOUND_COLORS.never, opacity: 0.7 }} />
                </>
              ) : (
                <text x={b0} y={y + 4} className="content-caption" style={MUTED}>—</text>
              )
            ) : (
              <text x={W - 8} y={y + 4} className="content-caption tabular-nums" style={INK} textAnchor="end">
                {fmt(row.mean)} <tspan style={MUTED}>± {fmt(row.sd)}</tspan>
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}
