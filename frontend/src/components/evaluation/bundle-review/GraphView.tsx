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

// Graph: the pair merged into one layered graph (report and campaign, who, how, then
// targets, software and infrastructure), each node and edge coloured by its status; what
// only the reference has is dashed.

import { useId, useMemo } from "react";
import { useFormatter } from "@/i18n";
import {
  GRAPH_NODE,
  GRAPH_WIDTH,
  GRAPH_X,
  displayStatus,
  entityVisible,
  graphLayout,
  relationshipVisible,
  type DisplayStatus,
} from "@/lib/evaluation-bundle-review";
import { STATUS_COLOR, statusLabel } from "./labels";
import { edgeLabel, nodeName, nodeSub } from "./graph-text";
import { isSelected, onActivate, type ReviewContext } from "./context";

const trunc = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

/** One arrowhead per status, drawn in its colour. */
export function StatusMarkers({ prefix, refX }: { prefix: string; refX: number }) {
  return (
    <defs>
      {(Object.keys(STATUS_COLOR) as DisplayStatus[]).map((st) => (
        <marker key={st} id={`${prefix}-${st}`} viewBox="0 0 10 10" refX={refX} refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" style={{ fill: STATUS_COLOR[st] }} />
        </marker>
      ))}
    </defs>
  );
}

/** The legend's words, read by `t_i18n`. @i18n-keys */
const LEGEND_LABELS = {
  fixed: "Fixed",
  regressed: "Regressed",
  new: "New error",
  still: "Still wrong",
  same: "Unchanged",
  onlyRef: "Only in the reference",
  correct: "Correct",
  notExpected: "Not expected",
  otherForm: "In the expected bundle in another form",
  matched: "Matched",
  missing: "Missing",
  spurious: "Spurious",
  broken: "Broken by an entity near-miss",
  wrongType: "Wrong relationship type",
  whatIf: "Matched in the what-if",
  correctWhatIf: "Correct in the what-if",
} as const;

/** What each colour and line means, for the mode on screen. */
export function GraphLegend({ ctx }: { ctx: ReviewContext }) {
  const { t_i18n } = useFormatter();
  const L = LEGEND_LABELS;
  const entries: [DisplayStatus, boolean, string][] =
    ctx.mode === "runs"
      ? [["fixed", false, L.fixed], ["regressed", false, L.regressed], ["new", false, L.new], ["still", false, L.still], ["same", false, L.same], ["same", true, L.onlyRef]]
      : ctx.mode === "alone"
        ? [["tp", false, L.correct], ["fp", false, L.notExpected], ["near", false, L.otherForm]]
        : [["tp", false, L.matched], ["fn", true, L.missing], ["fp", false, L.spurious], ["near", true, L.broken], ["type", false, L.wrongType]];
  if (ctx.whatIf && ctx.mode !== "runs") entries.push(["sim", true, ctx.mode === "alone" ? L.correctWhatIf : L.whatIf]);
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 content-caption text-default-secondary" aria-label={t_i18n("Legend")}>
      {entries.map(([st, dashed, label]) => (
        <li key={label} className="flex items-center gap-1.5">
          <svg viewBox="0 0 28 10" className="h-2.5 w-7" aria-hidden>
            <line x1="0" x2="28" y1="5" y2="5" strokeWidth="2" strokeDasharray={dashed ? "5 3" : undefined} style={{ stroke: STATUS_COLOR[st] }} />
          </svg>
          {t_i18n(label)}
        </li>
      ))}
    </ul>
  );
}

/** The graph's column titles, read by `t_i18n`. @i18n-keys */
const COLUMN_TITLES = ["Report and campaign", "Who", "How", "Targets, software and infrastructure"] as const;

export function GraphView({ ctx }: { ctx: ReviewContext }) {
  const { t_i18n } = useFormatter();
  const prefix = useId().replace(/:/g, "");
  const { derived, mode, whatIf, filters, model } = ctx;
  const { pos, height } = useMemo(() => graphLayout(derived.entItems.map((it) => it.g)), [derived.entItems]);
  const selE = ctx.selection?.kind === "e" ? ctx.selection.id : null;
  const { w: NW, h: NH } = GRAPH_NODE;
  return (
    <div className="space-y-2">
      <p className="content-compact text-default-secondary">
        {mode === "alone"
          ? t_i18n("The answer on its own, coloured against the expected bundle.")
          : t_i18n("Both sides merged into one graph; dashed: only in the reference. Select a node to name its relationships.")}
      </p>
      <div className="overflow-auto rounded-sm border border-elevation-subtle">
        <svg viewBox={`0 0 ${GRAPH_WIDTH} ${height}`} className="min-w-[48rem] w-full" role="group" aria-label={t_i18n("Merged graph of the compared bundles")}>
          <StatusMarkers prefix={prefix} refX={9} />
          {COLUMN_TITLES.map((title, i) => (
            <text key={title} x={GRAPH_X[i] - NW / 2} y={22} fontSize={11} style={{ fill: "var(--text-default-secondary)" }}>
              {t_i18n(title).toUpperCase()}
            </text>
          ))}
          {derived.relItems.map((it) => {
            const a = pos.get(it.r.src);
            const b = pos.get(it.r.tgt);
            if (!a || !b) return null;
            const st = displayStatus(it.st, mode, whatIf);
            let d: string;
            let lx: number;
            let ly: number;
            if (Math.abs(a.x - b.x) < 1) {
              const x = a.x + NW / 2;
              d = `M${x},${a.y} C${x + 46},${a.y} ${x + 46},${b.y} ${x},${b.y}`;
              lx = x + 34;
              ly = (a.y + b.y) / 2;
            } else {
              const [p, q] = a.x < b.x ? [a, b] : [b, a];
              const x1 = p.x + NW / 2;
              const x2 = q.x - NW / 2;
              const mx = (x1 + x2) / 2;
              d = a.x < b.x ? `M${x1},${p.y} C${mx},${p.y} ${mx},${q.y} ${x2},${q.y}` : `M${x2},${q.y} C${mx},${q.y} ${mx},${p.y} ${x1},${p.y}`;
              lx = mx;
              ly = (a.y + b.y) / 2;
            }
            const label = edgeLabel(ctx, it, "both", t_i18n);
            const selected = isSelected(ctx, "r", it.r.id);
            const shown = selected || (selE && (it.r.src === selE || it.r.tgt === selE));
            const visible = relationshipVisible(model, it, filters, mode);
            const select = () => ctx.select({ kind: "r", id: it.r.id });
            return (
              <g
                key={it.r.id}
                role="button"
                tabIndex={visible ? 0 : -1}
                aria-label={t_i18n("Relationship {label}, {status}", { label, status: t_i18n(statusLabel(st, mode)) })}
                onClick={select}
                onKeyDown={onActivate(select)}
                className="cursor-pointer focus-visible:outline-hidden"
                opacity={visible ? 1 : 0.15}
              >
                <title>{`${label} · ${t_i18n(statusLabel(st, mode))}`}</title>
                <path d={d} fill="none" strokeWidth={selected ? 2.5 : 1.4} strokeDasharray={it.inB ? undefined : "6 4"} markerEnd={`url(#${prefix}-${st})`} style={{ stroke: STATUS_COLOR[st] }} />
                <path d={d} fill="none" strokeWidth={12} style={{ stroke: "transparent" }} />
                {shown && label && (
                  <g>
                    <rect x={lx - (label.length * 6.4 + 12) / 2} y={ly - 9} width={label.length * 6.4 + 12} height={18} rx={4} style={{ fill: "var(--bg-elevation-default)", stroke: STATUS_COLOR[st] }} />
                    <text x={lx} y={ly + 3.5} textAnchor="middle" fontSize={11} style={{ fill: "var(--text-default-primary)" }}>
                      {label}
                    </text>
                  </g>
                )}
              </g>
            );
          })}
          {derived.entItems.map((it) => {
            const p = pos.get(it.g.id);
            if (!p) return null;
            const st = displayStatus(it.st, mode, whatIf);
            const name = nodeName(it);
            const sub = nodeSub(ctx, it, t_i18n);
            const selected = selE === it.g.id;
            const visible = entityVisible(it, filters, mode);
            const select = () => ctx.select({ kind: "e", id: it.g.id });
            return (
              <g
                key={it.g.id}
                role="button"
                tabIndex={visible ? 0 : -1}
                aria-pressed={selected}
                aria-label={`${it.g.type} ${name}, ${t_i18n(statusLabel(st, mode))}`}
                onClick={select}
                onKeyDown={onActivate(select)}
                className="cursor-pointer focus-visible:outline-hidden"
                opacity={visible ? 1 : 0.2}
              >
                <title>{name}</title>
                <rect
                  x={p.x - NW / 2}
                  y={p.y - NH / 2}
                  width={NW}
                  height={NH}
                  rx={7}
                  strokeWidth={selected ? 3 : 1.5}
                  strokeDasharray={it.b || mode === "alone" ? undefined : "5 3"}
                  style={{ fill: "var(--bg-elevation-default)", stroke: STATUS_COLOR[st] }}
                />
                <text x={p.x - NW / 2 + 9} y={p.y - 10} fontSize={10} style={{ fill: "var(--text-default-secondary)" }}>
                  {it.g.type}
                </text>
                <text x={p.x - NW / 2 + 9} y={sub ? p.y + 4 : p.y + 9} fontSize={12} style={{ fill: "var(--text-default-primary)" }}>
                  {trunc(name, 22)}
                </text>
                {sub && (
                  <text x={p.x - NW / 2 + 9} y={p.y + 18} fontSize={10} style={{ fill: STATUS_COLOR[st] }}>
                    {trunc(sub, 26)}
                  </text>
                )}
              </g>
            );
          })}
        </svg>
      </div>
      <GraphLegend ctx={ctx} />
    </div>
  );
}
