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
 * What the dashboard's chart renderers share: axis and tooltip styles from the
 * design-system tokens, the legend's placement, a measured width.
 */
import { createContext, useContext, useEffect, useRef, useState, type KeyboardEvent } from "react";

import { formatCompactNumber } from "@/lib/utils";

import { truncateLabel, type LegendDisplay, type LegendTruncation } from "./chart-options";
import type { DosScope } from "./dynamic-options";
import type { Look } from "./look";
import type { ClickEvent } from "../interactions";

/** What a click or a brush on a chart reports (the dashboard's interactions). */
export interface ChartEvents {
  onSelect?: (event: ClickEvent) => void;
  onBrush?: (range: { start: string; end: string }) => void;
}
export const ChartEventsContext = createContext<ChartEvents>({});

export function useChartEvents(): ChartEvents {
  return useContext(ChartEventsContext);
}

/** A clickable element's props when the chart reports clicks: a pointer, and Enter for the keyboard. */
export function clickable(onSelect: ChartEvents["onSelect"], event: ClickEvent) {
  if (!onSelect) return {};
  return {
    role: "button",
    tabIndex: 0,
    className: "cursor-pointer",
    onClick: () => onSelect(event),
    onKeyDown: (key: KeyboardEvent) => {
      if (key.key === "Enter") onSelect(event);
    },
  };
}

/** A brush's span as `onBrush` reports it: the first and last x, once the brush settles (not at every pixel of a drag). */
export function useBrushReport(rows: readonly { x?: unknown }[], onBrush: ChartEvents["onBrush"]) {
  const [span, setSpan] = useState<{ startIndex?: number; endIndex?: number } | null>(null);
  const reported = useRef("");
  useEffect(() => {
    if (!onBrush || span?.startIndex === undefined || span.endIndex === undefined) return;
    const timer = setTimeout(() => {
      const start = rows[span.startIndex as number]?.x;
      const end = rows[span.endIndex as number]?.x;
      const key = `${start}|${end}`;
      if (start !== undefined && end !== undefined && key !== reported.current) {
        reported.current = key;
        onBrush({ start: String(start), end: String(end) });
      }
    }, 400);
    return () => clearTimeout(timer);
  }, [span, onBrush, rows]);
  return setSpan;
}

export const AXIS_TICK = { fill: "var(--text-default-secondary)", fontSize: 11 } as const;
export const AXIS_STROKE = "var(--border-elevation-subtle)";
export const GRID_STROKE = "var(--border-elevation-subtle)";
export const LABEL_STYLE = { fill: "var(--text-default-secondary)", fontSize: 11 } as const;

/** A tooltip that names what is under the pointer and nothing else (`tooltipContent: "label"`):
 *  the page's background, the text colour, 2 by 6 px, 12 px. */
export const LABEL_TIP_STYLE = {
  backgroundColor: "var(--bg-elevation-default-layer-0)",
  color: "var(--text-default-primary)",
  padding: "2px 6px",
  fontSize: 12,
} as const;

export const compact = (value: unknown) => formatCompactNumber(Number(value));

/** The props of a recharts `<Legend>` placed as `legendDisplay` asks, or `null` when it is off;
 *  its marker and text colour as the look asks (`legendMarker`, `legendLabelColor`). */
export function legendProps(display: LegendDisplay, truncation: LegendTruncation, look?: Pick<Look, "legendMarker" | "legendLabelColor">) {
  if (display === "off") return null;
  const side = display === "left" || display === "right";
  const color = look?.legendLabelColor;
  return {
    layout: side ? ("vertical" as const) : ("horizontal" as const),
    align: side ? display : ("center" as const),
    verticalAlign: side ? ("middle" as const) : display,
    wrapperStyle: { fontSize: 11, color: "var(--text-default-secondary)" },
    ...(look?.legendMarker ? { iconType: look.legendMarker } : {}),
    // In the data's order: recharts sorts the items by name unless told not to.
    itemSorter: null,
    formatter: (value: unknown) => {
      const text = truncateLabel(String(value ?? ""), side ? 20 : 28, truncation);
      return color ? <span style={{ color }}>{text}</span> : text;
    },
  };
}

/** The width of the element `ref` points at, kept current as it resizes. */
export function useElementWidth<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const node = ref.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => setWidth(Math.round(entries[0]?.contentRect.width ?? 0)));
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  return { ref, width };
}

/** What every renderer is handed: the closed data, its frame and the options to read. */
export interface ChartProps {
  shape: "number" | "series" | "distribution" | "rows" | "outcomes";
  data: unknown;
  scope: DosScope & { options: Record<string, unknown> };
  height: number;
}
