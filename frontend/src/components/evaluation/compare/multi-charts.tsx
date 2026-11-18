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
 * The charts of the evaluations comparison (#12282): the same model in two evaluations,
 * entry by entry (dumbbell: a hollow dot is an answer the scorer could not read), and
 * every configuration ranked with its 95% interval (forest plot: its runs and the
 * evaluation's default model named). Data-viz SVG drawn with design tokens only: colours
 * are CSS variables, text takes the type scale classes. `saveSvg` writes a chart as a
 * file, its variables resolved so it reads the same outside the page.
 */

import { useFormatter } from "@/i18n";
import { saveBlobAs } from "@/lib/utils";
import { signed } from "@/lib/evaluation-comparability";

import { EVALUATION_COLORS } from "./compare-colors";

const GRID = { stroke: "var(--border-elevation-subtle)" };
const MUTED = { fill: "var(--text-default-secondary)" };
const INK = { fill: "var(--text-default-primary)" };
const PAPER = "var(--bg-elevation-default)";
const TICKS = [0, 25, 50, 75, 100];

const fmt = (v: number) => v.toFixed(1);
const trunc = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

/** Saves the chart inside `container` as an SVG file, every `var(--x)` resolved. */
export function saveSvg(container: HTMLElement | null, filename: string): boolean {
  const svg = container?.querySelector("svg");
  if (!svg) return false;
  const css = getComputedStyle(document.documentElement);
  const resolve = (text: string) =>
    text.replace(/var\(--([a-z0-9-]+)\)/gi, (_, name: string) => css.getPropertyValue(`--${name}`).trim() || "currentColor");
  const clone = svg.cloneNode(true) as SVGSVGElement;
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  clone.querySelectorAll<SVGElement>("[style]").forEach((el) => el.setAttribute("style", resolve(el.getAttribute("style") ?? "")));
  const text = new XMLSerializer().serializeToString(clone);
  saveBlobAs(new Blob([text], { type: "image/svg+xml;charset=utf-8" }), filename);
  return true;
}

interface PairRow {
  key: string;
  label: string;
  before: number;
  after: number;
  /** The scorer could not read that side's answer: everything counted missing. */
  beforeUnreadable?: boolean;
  afterUnreadable?: boolean;
}

/** The same model in two evaluations, entry by entry. `onOpen` makes a row a button. */
export function PairedDumbbell({
  rows,
  metricLabel,
  beforeLabel,
  afterLabel,
  colors,
  onOpen,
}: {
  rows: PairRow[];
  metricLabel: string;
  beforeLabel: string;
  afterLabel: string;
  colors: [string, string];
  onOpen?: (key: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const W = 720, top = 28, rh = 34, H = top + rows.length * rh + 30, x0 = 250, x1 = 640;
  const X = (v: number) => x0 + ((x1 - x0) * v) / 100;
  const side = (label: string, value: number, unreadable?: boolean) =>
    unreadable
      ? t_i18n("{source} {value} (no readable answer)", { source: label, value: fmt(value) })
      : t_i18n("{source} {value}", { source: label, value: fmt(value) });
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="w-full max-w-4xl"
      role="group"
      aria-label={t_i18n("{metric} entry by entry: {before} against {after}", { metric: metricLabel, before: beforeLabel, after: afterLabel })}
    >
      <rect width={W} height={H} style={{ fill: PAPER }} />
      <text x={8} y={16} className="content-caption" style={MUTED}>{t_i18n("Entry")}</text>
      <text x={x0} y={16} className="content-caption" style={MUTED}>{metricLabel}</text>
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
        const name = t_i18n("{entry}: {before}, {after}.", {
          entry: row.label,
          before: side(beforeLabel, row.before, row.beforeUnreadable),
          after: side(afterLabel, row.after, row.afterUnreadable),
        });
        const open = onOpen ? () => onOpen(row.key) : undefined;
        const label = open ? t_i18n("{name} Click to diff them on this entry.", { name }) : name;
        return (
          <g
            key={row.key}
            role={open ? "button" : undefined}
            tabIndex={open ? 0 : undefined}
            aria-label={label}
            className={open ? "cursor-pointer" : undefined}
            onClick={open}
            onKeyDown={open ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } } : undefined}
          >
            <title>{label}</title>
            <rect x={0} y={y - rh / 2 + 2} width={W} height={rh - 4} style={{ fill: "transparent" }} />
            <text x={8} y={y + 4} className="content-caption" style={INK}>{trunc(row.label, 32)}</text>
            <line x1={X(row.before)} x2={X(row.after)} y1={y} y2={y} strokeWidth={1.5} style={{ stroke: "var(--text-default-secondary)", opacity: 0.6 }} />
            <circle cx={X(row.before)} cy={y} r={5.5} strokeWidth={2} style={{ fill: row.beforeUnreadable ? PAPER : colors[0], stroke: colors[0] }} />
            <circle cx={X(row.after)} cy={y} r={5.5} strokeWidth={2} style={{ fill: row.afterUnreadable ? PAPER : colors[1], stroke: colors[1] }} />
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

interface RankedRow {
  id: string;
  label: string;
  value: number;
  low: number;
  high: number;
  best: number;
  worst: number;
  /** Index in `EVALUATION_COLORS`. */
  colorIndex: number;
  /** Runs pooled into the figure. */
  runs: number;
  /** The run is on its evaluation's default model. */
  isDefault?: boolean;
}

/** Every configuration ranked with its 95% interval. */
export function RankedForest({ rows, metricLabel }: { rows: RankedRow[]; metricLabel: string }) {
  const { t_i18n } = useFormatter();
  const W = 760, top = 28, rh = 24, H = top + rows.length * rh + 30, x0 = 330, x1 = 620;
  const X = (v: number) => x0 + ((x1 - x0) * v) / 100;
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="w-full max-w-4xl"
      role="img"
      aria-label={t_i18n("{metric} of each configuration with its 95% interval", { metric: metricLabel })}
    >
      <rect width={W} height={H} style={{ fill: PAPER }} />
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
        const name = row.isDefault ? t_i18n("{label} · default", { label: row.label }) : row.label;
        return (
          <g key={row.id}>
            <title>
              {t_i18n("{label}: {value} (95% interval {low} to {high}), rank {rank}, {runs, plural, one {# run} other {# runs}}", {
                label: name, value: fmt(row.value), low: fmt(row.low), high: fmt(row.high), rank, runs: row.runs,
              })}
            </title>
            <text x={8} y={y + 4} className="content-caption tabular-nums" style={INK}>{rank}</text>
            <text x={56} y={y + 4} className="content-caption" style={INK}>
              {trunc(row.label, 40)}
              {row.isDefault && <tspan style={MUTED}> · {t_i18n("default")}</tspan>}
            </text>
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
