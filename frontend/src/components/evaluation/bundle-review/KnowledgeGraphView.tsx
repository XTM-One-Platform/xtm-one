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

// Knowledge graph: the pair as OpenCTI draws a knowledge graph (an icon per STIX type, a
// force layout, labelled edges), each node ringed with its status. The layout is computed
// once per entry; the reader zooms (wheel or buttons), pans and drags nodes.

import { useEffect, useId, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { Icon, IconButton, type IconName } from "@filigran/design-system";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useFormatter } from "@/i18n";
import {
  EXPECTED,
  displayStatus,
  entityVisible,
  fitBox,
  relationshipVisible,
  zoomBox,
  type Box,
  type Point,
} from "@/lib/evaluation-bundle-review";
import { STATUS_COLOR, statusLabel, typeColor } from "./labels";
import { GraphLegend, StatusMarkers } from "./GraphView";
import { edgeLabel, nodeName, nodeSub } from "./graph-text";
import { onActivate, type ReviewContext } from "./context";

export type GraphSide = "a" | "both" | "b";

const TYPE_ICON: Record<string, IconName> = {
  "attack-pattern": "custom/attack-pattern",
  "intrusion-set": "custom/intrusion-set",
  "threat-actor": "custom/threat-actor-individual",
  malware: "custom/malware",
  tool: "wrench",
  software: "box",
  campaign: "flag",
  vulnerability: "bug",
  identity: "building-2",
  location: "map-pin",
  "domain-name": "earth",
  "ipv4-addr": "network",
  "ipv6-addr": "network",
  url: "link-2",
  file: "file",
  indicator: "radar",
  report: "file-text",
  infrastructure: "custom/infrastructure",
  "course-of-action": "custom/course-of-action",
};

const R0 = 22;
const trunc = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

interface Drag {
  /** The node dragged; none: the view is panned. */
  id: string | null;
  /** The edge under the pointer, selected on a click. */
  edge: string | null;
  start: Point;
  origin: Point;
  box: Box;
  moved: number;
}

export function KnowledgeGraphView({
  ctx,
  layout,
  side,
  onSide,
}: {
  ctx: ReviewContext;
  /** The entry's force layout, computed once per entry. */
  layout: Map<string, Point>;
  side: GraphSide;
  onSide: (side: GraphSide) => void;
}) {
  const { t_i18n } = useFormatter();
  const prefix = useId().replace(/:/g, "");
  const { derived, mode, whatIf, filters, model } = ctx;
  const [moved, setMoved] = useState<Map<string, Point>>(() => new Map());
  const pos = (id: string) => moved.get(id) ?? layout.get(id) ?? { x: 0, y: 0 };
  const fitted = useMemo(
    () => fitBox(derived.entItems.map((it) => layout.get(it.g.id)).filter((p): p is Point => !!p)),
    [derived.entItems, layout],
  );
  const [box, setBox] = useState<Box>(fitted);
  // A new pair or new filters fit the view again (state adjusted while rendering, not in an effect).
  const [fittedFor, setFittedFor] = useState(fitted);
  if (fittedFor !== fitted) {
    setFittedFor(fitted);
    setBox(fitted);
  }
  const svgRef = useRef<SVGSVGElement>(null);
  const drag = useRef<Drag | null>(null);
  const effective: GraphSide = mode === "alone" ? "b" : side;

  const toSvg = (clientX: number, clientY: number): Point => {
    const svg = svgRef.current;
    const ctm = svg?.getScreenCTM();
    if (!svg || !ctm) return { x: clientX, y: clientY };
    const pt = new DOMPoint(clientX, clientY).matrixTransform(ctm.inverse());
    return { x: pt.x, y: pt.y };
  };

  // The wheel zooms around the pointer; a passive React listener could not stop the page from scrolling.
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheel = (ev: WheelEvent) => {
      ev.preventDefault();
      const ctm = svg.getScreenCTM();
      const p = ctm ? new DOMPoint(ev.clientX, ev.clientY).matrixTransform(ctm.inverse()) : null;
      setBox((b) => zoomBox(b, ev.deltaY > 0 ? 1.12 : 1 / 1.12, p?.x, p?.y));
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, []);

  const onPointerDown = (ev: ReactPointerEvent<SVGSVGElement>) => {
    // Read before the capture: every later event of this pointer targets the svg itself.
    const node = (ev.target as Element).closest<SVGGElement>("[data-node]");
    const id = node?.dataset.node ?? null;
    const edge = id ? null : (ev.target as Element).closest<SVGGElement>("[data-edge]")?.dataset.edge ?? null;
    drag.current = { id, edge, start: toSvg(ev.clientX, ev.clientY), origin: id ? pos(id) : { x: ev.clientX, y: ev.clientY }, box, moved: 0 };
    ev.currentTarget.setPointerCapture?.(ev.pointerId);
  };
  const onPointerMove = (ev: ReactPointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    if (!d) return;
    if (d.id) {
      const p = toSvg(ev.clientX, ev.clientY);
      const next = { x: d.origin.x + p.x - d.start.x, y: d.origin.y + p.y - d.start.y };
      d.moved = Math.abs(p.x - d.start.x) + Math.abs(p.y - d.start.y);
      if (d.moved > 3) setMoved((m) => new Map(m).set(d.id as string, next));
    } else {
      const svg = svgRef.current;
      const k = svg && svg.clientWidth ? d.box.w / svg.clientWidth : 1;
      const dx = (ev.clientX - d.origin.x) * k;
      const dy = (ev.clientY - d.origin.y) * k;
      d.moved = Math.abs(ev.clientX - d.origin.x) + Math.abs(ev.clientY - d.origin.y);
      if (d.moved > 3) setBox({ ...d.box, x: d.box.x - dx, y: d.box.y - dy });
    }
  };
  const onPointerUp = () => {
    const d = drag.current;
    drag.current = null;
    if (!d || d.moved > 3) return;
    if (d.id) ctx.select({ kind: "e", id: d.id });
    else if (d.edge) ctx.select({ kind: "r", id: d.edge });
  };

  const selE = ctx.selection?.kind === "e" ? ctx.selection.id : null;
  const selR = ctx.selection?.kind === "r" ? ctx.selection.id : null;
  const help =
    mode === "alone"
      ? t_i18n("The answer on its own, ringed against the expected bundle.")
      : effective === "both"
        ? t_i18n("Both sides in one knowledge graph: the ring is the status, the icon the STIX type.")
        : t_i18n("{side} only ({name}). What only {other} has stays as a ghost, at the same place.", {
            side: effective === "a" ? (ctx.pair.ref === EXPECTED ? t_i18n("Expected") : ctx.letter(ctx.pair.ref)) : ctx.letter(ctx.pair.under),
            name: ctx.sourceName(effective === "a" ? ctx.pair.ref : ctx.pair.under),
            other: effective === "a" ? ctx.letter(ctx.pair.under) : ctx.pair.ref === EXPECTED ? t_i18n("the expected bundle") : ctx.letter(ctx.pair.ref),
          });
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="content-compact text-default-secondary">
          {help} {t_i18n("Drag nodes, scroll to zoom.")}
        </p>
        {mode !== "alone" && (
          <SegmentedFilter
            selection="radio"
            size="sm"
            ariaLabel={t_i18n("Bundle shown")}
            value={side}
            onChange={onSide}
            options={[
              { key: "a", label: ctx.pair.ref === EXPECTED ? "Expected" : ctx.letter(ctx.pair.ref) },
              { key: "both", label: "Both" },
              { key: "b", label: ctx.letter(ctx.pair.under) },
            ]}
          />
        )}
      </div>
      <div className="relative rounded-sm border border-elevation-subtle">
        <svg
          ref={svgRef}
          viewBox={`${box.x} ${box.y} ${box.w} ${box.h}`}
          className="h-[36rem] w-full touch-none select-none"
          role="group"
          aria-label={t_i18n("Knowledge graph of the compared bundles")}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={() => (drag.current = null)}
        >
          <StatusMarkers prefix={prefix} refX={8} />
          {derived.relItems.map((it) => {
            const a = pos(it.r.src);
            const b = pos(it.r.tgt);
            const st = displayStatus(it.st, mode, whatIf);
            const dx = b.x - a.x;
            const dy = b.y - a.y;
            const d = Math.max(Math.hypot(dx, dy), 1);
            const ux = dx / d;
            const uy = dy / d;
            const x1 = a.x + ux * (R0 + 6);
            const y1 = a.y + uy * (R0 + 6);
            const x2 = b.x - ux * (R0 + 8);
            const y2 = b.y - uy * (R0 + 8);
            const ghost = effective === "a" ? !it.inA : effective === "b" ? !it.inB : false;
            const label = edgeLabel(ctx, it, ghost ? "both" : effective, t_i18n);
            let angle = (Math.atan2(dy, dx) * 180) / Math.PI;
            if (angle > 90 || angle < -90) angle += 180;
            const mx = (x1 + x2) / 2;
            const my = (y1 + y2) / 2;
            const visible = relationshipVisible(model, it, filters, mode);
            const select = () => ctx.select({ kind: "r", id: it.r.id });
            return (
              <g
                key={it.r.id}
                data-edge={it.r.id}
                role="button"
                tabIndex={visible && !ghost ? 0 : -1}
                aria-label={t_i18n("Relationship {label}, {status}", { label, status: t_i18n(statusLabel(st, mode)) })}
                onKeyDown={onActivate(select)}
                className="cursor-pointer focus-visible:outline-hidden"
                opacity={ghost ? 0.1 : visible ? 1 : 0.15}
              >
                <title>{`${label} · ${t_i18n(statusLabel(st, mode))}`}</title>
                <line x1={x1} y1={y1} x2={x2} y2={y2} strokeWidth={selR === it.r.id ? 2.5 : 1.3} strokeDasharray={it.inB ? undefined : "6 4"} markerEnd={`url(#${prefix}-${st})`} style={{ stroke: STATUS_COLOR[st] }} />
                <line x1={x1} y1={y1} x2={x2} y2={y2} strokeWidth={12} style={{ stroke: "transparent" }} />
                <text x={mx} y={my - 4} textAnchor="middle" fontSize={10} transform={`rotate(${angle} ${mx} ${my})`} style={{ fill: "var(--text-default-secondary)" }}>
                  {label}
                </text>
              </g>
            );
          })}
          {derived.entItems.map((it) => {
            const p = pos(it.g.id);
            const st = displayStatus(it.st, mode, whatIf);
            const ghost = effective === "a" ? !it.a : effective === "b" ? !it.b : false;
            const nameSide = effective === "both" ? "both" : effective;
            const name = nodeName(it, nameSide);
            const sub = nodeSub(ctx, it, t_i18n, nameSide);
            const visible = entityVisible(it, filters, mode);
            const color = typeColor(it.g.type);
            const select = () => ctx.select({ kind: "e", id: it.g.id });
            return (
              <g
                key={it.g.id}
                data-node={it.g.id}
                transform={`translate(${p.x} ${p.y})`}
                role="button"
                tabIndex={visible && !ghost ? 0 : -1}
                aria-pressed={selE === it.g.id}
                aria-label={`${it.g.type} ${name}, ${t_i18n(statusLabel(st, mode))}`}
                onKeyDown={onActivate(select)}
                className="cursor-pointer focus-visible:outline-hidden"
                opacity={ghost ? 0.13 : visible ? 1 : 0.2}
              >
                <title>{`${it.g.type} · ${name} · ${t_i18n(statusLabel(st, mode))}`}</title>
                {selE === it.g.id && <circle r={R0 + 8} fillOpacity={0.25} style={{ fill: STATUS_COLOR[st] }} />}
                <circle r={R0 + 3.5} fill="none" strokeWidth={2.5} strokeDasharray={it.b || mode === "alone" ? undefined : "5 3"} style={{ stroke: STATUS_COLOR[st] }} />
                <circle r={R0} strokeWidth={1} style={{ fill: "var(--bg-elevation-default)", stroke: color }} />
                <g transform="translate(-10 -10)" className="text-default-primary">
                  <Icon name={TYPE_ICON[it.g.type] ?? "circle"} size={20} />
                </g>
                <text y={R0 + 19} textAnchor="middle" fontSize={11} style={{ fill: "var(--text-default-primary)" }}>
                  {trunc(name, 26)}
                </text>
                {sub && (
                  <text y={R0 + 32} textAnchor="middle" fontSize={10} style={{ fill: STATUS_COLOR[st] }}>
                    {trunc(sub, 28)}
                  </text>
                )}
              </g>
            );
          })}
        </svg>
        <div className="absolute right-2 top-2 flex flex-col gap-1">
          <IconButton size="sm" aria-label={t_i18n("Zoom in")} icon={<Icon name="plus" size={16} />} onClick={() => setBox((b) => zoomBox(b, 1 / 1.25))} />
          <IconButton size="sm" aria-label={t_i18n("Zoom out")} icon={<Icon name="minus" size={16} />} onClick={() => setBox((b) => zoomBox(b, 1.25))} />
          <IconButton size="sm" aria-label={t_i18n("Fit to screen")} icon={<Icon name="expand" size={16} />} onClick={() => setBox(fitted)} />
          <IconButton
            size="sm"
            aria-label={t_i18n("Reset the layout")}
            icon={<Icon name="rotate-ccw" size={16} />}
            onClick={() => {
              setMoved(new Map());
              setBox(fitted);
            }}
          />
        </div>
      </div>
      <GraphLegend ctx={ctx} />
    </div>
  );
}
