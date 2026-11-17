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
 * The two charts of the evaluation comparison (#12282): a ranking with 95% intervals
 * (forest plot) and a per-entry change between two runs (dumbbell). Data-viz SVG drawn
 * with design tokens only: colours are CSS variables, text takes the type scale classes.
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
  const W = 720, top = 28, rh = 32, H = top + rows.length * rh + 30, x0 = 260, x1 = 640;
  const X = (v: number) => x0 + ((x1 - x0) * v) / 100;
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="w-full max-w-4xl"
      role="group"
      aria-label={t_i18n("{metric} entry by entry: {before} against {after}", { metric: metricLabel, before: beforeLabel, after: afterLabel })}
    >
      <text x={8} y={16} className="content-caption" style={MUTED}>{t_i18n("Entry")}</text>
      <text x={W - 8} y={16} className="content-caption" style={MUTED} textAnchor="end">{t_i18n("Change")}</text>
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
