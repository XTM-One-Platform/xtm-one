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
 * plot) and a per-entry change between two runs (dumbbell); of one evaluation's
 * Leaderboard and Results (#2): the best run of each launch, quality against cost or time
 * with the Pareto frontier, and how far reruns of one configuration spread. Data-viz SVG
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
const signed = (v: number) => `${v > 0.05 ? "+" : v < -0.05 ? "−" : "±"}${Math.abs(v).toFixed(1)}`;
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
      <text x={W - 8} y={16} className="content-caption" style={MUTED} textAnchor="end">{metricLabel}</text>
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
        return (
          <g key={row.id}>
            <title>
              {t_i18n("{label}: {value} (95% interval {low} to {high}), rank {rank}", {
                label: row.label, value: fmt(row.value), low: fmt(row.low), high: fmt(row.high), rank,
              })}
            </title>
            <text x={8} y={y + 4} className="content-caption tabular-nums" style={INK}>{rank}</text>
            <text x={56} y={y + 4} className="content-caption" style={INK}>{trunc(row.label, 44)}</text>
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

interface DumbbellRow {
  key: string;
  label: string;
  before: number;
  after: number;
}

/** The same model in two evaluations, entry by entry. `onOpen` makes a row a button. */
export function DumbbellChart({
  rows,
  metricLabel,
  beforeLabel,
  afterLabel,
  colors,
  onOpen,
}: {
  rows: DumbbellRow[];
  metricLabel: string;
  beforeLabel: string;
  afterLabel: string;
  colors: [string, string];
  onOpen?: (key: string) => void;
}) {
  const { t_i18n } = useFormatter();
  // A legend line (which dot is which side), then the column headings.
  const W = 720, top = 52, rh = 32, H = top + rows.length * rh + 30, x0 = 260, x1 = 640;
  const X = (v: number) => x0 + ((x1 - x0) * v) / 100;
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="w-full max-w-4xl"
      role="group"
      aria-label={t_i18n("{metric} entry by entry: {before} against {after}", { metric: metricLabel, before: beforeLabel, after: afterLabel })}
    >
      <circle cx={14} cy={12} r={5.5} style={{ fill: colors[0] }} />
      <text x={26} y={16} className="content-caption" style={INK}>{trunc(beforeLabel, 44)}</text>
      <circle cx={W / 2 + 6} cy={12} r={5.5} style={{ fill: colors[1] }} />
      <text x={W / 2 + 18} y={16} className="content-caption" style={INK}>{trunc(afterLabel, 44)}</text>
      <text x={8} y={40} className="content-caption" style={MUTED}>{t_i18n("Entry")}</text>
      <text x={W - 8} y={40} className="content-caption" style={MUTED} textAnchor="end">{t_i18n("Change")}</text>
      {TICKS.map((t) => (
        <g key={t}>
          <line x1={X(t)} x2={X(t)} y1={top - 4} y2={H - 26} style={GRID} />
          <text x={X(t)} y={H - 10} className="content-caption" style={MUTED} textAnchor="middle">{t}</text>
        </g>
      ))}
      {rows.map((row, i) => {
        const y = top + i * rh + rh / 2;
        const change = row.after - row.before;
        const name = t_i18n("{entry}: {before} {a}, {after} {b}", {
          entry: row.label, before: beforeLabel, a: fmt(row.before), after: afterLabel, b: fmt(row.after),
        });
        const open = onOpen ? () => onOpen(row.key) : undefined;
        return (
          <g
            key={row.key}
            role={open ? "button" : undefined}
            tabIndex={open ? 0 : undefined}
            aria-label={open ? t_i18n("{name}. Open both answers side by side.", { name }) : name}
            className={open ? "cursor-pointer" : undefined}
            onClick={open}
            onKeyDown={open ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } } : undefined}
          >
            <title>{name}</title>
            <rect x={0} y={y - rh / 2 + 2} width={W} height={rh - 4} style={{ fill: "transparent" }} />
            <text x={8} y={y + 4} className="content-caption" style={INK}>{trunc(row.label, 36)}</text>
            <line x1={X(row.before)} x2={X(row.after)} y1={y} y2={y} strokeWidth={1.5} style={{ stroke: "var(--text-default-secondary)", opacity: 0.6 }} />
            <circle cx={X(row.before)} cy={y} r={5.5} style={{ fill: colors[0] }} />
            <circle cx={X(row.after)} cy={y} r={5.5} style={{ fill: colors[1] }} />
            <text
              x={W - 8}
              y={y + 4}
              className="content-caption tabular-nums"
              textAnchor="end"
              style={{ fill: change > 0.05 ? "var(--color-feedback-success-primary)" : change < -0.05 ? "var(--color-feedback-error-primary)" : "var(--text-default-secondary)" }}
            >
              {signed(change)}
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

interface LaunchBar {
  key: string;
  /** The launch's short name under its bar (a date). */
  label: string;
  /** Its best run's figure; null when none of its runs has one. */
  value: number | null;
  /** The hover text: the launch, its best run and its runs. */
  title: string;
}

/** The best run of each launch, oldest first, on 0-100. */
export function LaunchBars({ bars, metricLabel }: { bars: LaunchBar[]; metricLabel: string }) {
  const { t_i18n } = useFormatter();
  const step = 92, left = 36, top = 12, plot = 96, W = Math.max(360, left + bars.length * step + 8), H = top + plot + 34;
  const Y = (v: number) => top + plot - (plot * v) / 100;
  return (
    <div className="overflow-x-auto">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width={W}
        className="max-w-none"
        role="img"
        aria-label={t_i18n("Best run of each launch, {metric}", { metric: metricLabel })}
      >
        {[0, 50, 100].map((t) => (
          <g key={t}>
            <line x1={left} x2={W - 4} y1={Y(t)} y2={Y(t)} style={GRID} />
            <text x={left - 6} y={Y(t) + 4} className="content-caption" style={MUTED} textAnchor="end">{t}</text>
          </g>
        ))}
        {bars.map((bar, i) => {
          const x = left + i * step + 10;
          return (
            <g key={bar.key}>
              <title>{bar.title}</title>
              {bar.value != null && (
                <rect x={x} y={Y(bar.value)} width={step - 20} height={Math.max(1, Y(0) - Y(bar.value))} rx={2} style={{ fill: ACCENT, opacity: 0.8 }} />
              )}
              <text x={x + (step - 20) / 2} y={H - 18} className="content-caption tabular-nums" style={MUTED} textAnchor="middle">{bar.label}</text>
              <text x={x + (step - 20) / 2} y={H - 4} className="content-caption tabular-nums" style={INK} textAnchor="middle">
                {bar.value == null ? "—" : fmt(bar.value)}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

interface ParetoPoint {
  id: string;
  label: string;
  /** Cost (USD) or seconds per entry. */
  x: number;
  value: number;
  low: number;
  high: number;
  onFront: boolean;
}

/** Each configuration's figure against its cost or time per entry; the frontier dashed,
 *  from the origin (running nothing costs nothing and finds nothing). */
export function ParetoChart({ points, axis, metricLabel }: { points: ParetoPoint[]; axis: "cost" | "time"; metricLabel: string }) {
  const { t_i18n } = useFormatter();
  const W = 760, H = 380, l = 52, r = 24, t = 16, b = 46;
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
        return (
          <g key={p.id}>
            <title>
              {t_i18n("{label}: {value} (95% interval {low} to {high}) for {x} per entry", {
                label: p.label, value: fmt(p.value), low: fmt(p.low), high: fmt(p.high), x: fmtX(p.x),
              })}
            </title>
            <line x1={X(p.x)} x2={X(p.x)} y1={Y(p.low)} y2={Y(p.high)} strokeWidth={1.2} style={{ stroke: color, opacity: 0.5 }} />
            <circle cx={X(p.x)} cy={Y(p.value)} r={p.onFront ? 5.5 : 4} style={{ fill: color }} />
            <text
              x={X(p.x) + (leftward ? -8 : 8)}
              y={Y(p.value) - 7}
              className="content-caption"
              style={p.onFront ? INK : MUTED}
              textAnchor={leftward ? "end" : "start"}
            >
              {trunc(p.label, 32)}
            </text>
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
}

/** Reruns of one configuration: a dot per run, the min–max band and the mean. */
export function ConsistencyChart({ rows, metricLabel }: { rows: SpreadRow[]; metricLabel: string }) {
  const { t_i18n } = useFormatter();
  const W = 760, top = 28, rh = 30, H = top + rows.length * rh + 30, x0 = 300, x1 = 640;
  const X = (v: number) => x0 + ((x1 - x0) * v) / 100;
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="w-full max-w-4xl"
      role="img"
      aria-label={t_i18n("{metric} of each rerun of a configuration", { metric: metricLabel })}
    >
      <text x={8} y={16} className="content-caption" style={MUTED}>{t_i18n("Configuration · runs")}</text>
      <text x={W - 8} y={16} className="content-caption" style={MUTED} textAnchor="end">{t_i18n("Mean · sd")}</text>
      {TICKS.map((v) => (
        <g key={v}>
          <line x1={X(v)} x2={X(v)} y1={top - 4} y2={H - 26} style={GRID} />
          <text x={X(v)} y={H - 10} className="content-caption" style={MUTED} textAnchor="middle">{v}</text>
        </g>
      ))}
      {rows.map((row, i) => {
        const y = top + i * rh + rh / 2;
        return (
          <g key={row.id}>
            <title>
              {t_i18n("{label}: mean {mean}, sd {sd}, {min} to {max} over {count, plural, one {# run} other {# runs}}", {
                label: row.label, mean: fmt(row.mean), sd: fmt(row.sd), min: fmt(row.min), max: fmt(row.max), count: row.values.length,
              })}
            </title>
            <text x={8} y={y + 4} className="content-caption" style={INK}>
              {trunc(row.label, 40)} <tspan style={MUTED}>· {row.values.length}</tspan>
            </text>
            <rect x={X(row.min)} y={y - 7} width={Math.max(1, X(row.max) - X(row.min))} height={14} rx={3} style={{ fill: ACCENT, opacity: 0.14 }} />
            {row.values.map((v, k) => (
              <circle key={k} cx={X(v)} cy={y} r={2.8} style={{ fill: ACCENT, opacity: 0.6 }} />
            ))}
            <line x1={X(row.mean)} x2={X(row.mean)} y1={y - 9} y2={y + 9} strokeWidth={2} style={{ stroke: "var(--text-default-primary)" }} />
            <text x={W - 8} y={y + 4} className="content-caption tabular-nums" style={INK} textAnchor="end">
              {fmt(row.mean)} <tspan style={MUTED}>± {fmt(row.sd)}</tspan>
            </text>
          </g>
        );
      })}
    </svg>
  );
}
