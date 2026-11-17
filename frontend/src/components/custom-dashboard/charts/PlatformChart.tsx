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
 * The drawing of a live widget's data, by shape and view, with its
 * visualization's options.
 *
 * Loaded lazily (recharts stays out of the pages without a live widget). The
 * data is the server's closed shapes (`dashboard_live`): every string is text,
 * drawn as text; the only links are the row links the server built from the
 * integration's base URL, and they pass `safeExternalUrl` again here. The
 * options are read by `chart-options.ts`, their expressions by
 * `dynamic-options.ts`, on that data only.
 *
 * With `onSelect`, a click on a value, a point, a slice, a bar or a row says
 * what was clicked (the dashboard's interactions); with `onBrush`, a series
 * gets a brush whose span is reported when it settles. An answer in words
 * (the `text` shape) is drawn as its paragraphs.
 */
import type { ReactNode } from "react";

import { SampleBadge } from "@/components/ui/sample-badge";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { WidgetChartData, WidgetData, WidgetDistributionData, WidgetOutcomesData, WidgetRowsData } from "@/types";

import type { DataView, PlatformView } from "../spec";
import { ChartEventsContext, type ChartEvents, type ChartProps } from "./common";
import DataTable from "./DataTable";
import DistributionList from "./DistributionList";
import { frameOf, type DosScope } from "./dynamic-options";
import { isOutcomeView } from "../viz/outcomes";
import OutcomeViews from "./OutcomeViews";
import PieChart from "./PieChart";
import { emptyNote, isAllEmpty, readLook, sampleFor } from "./look";
import { VizNote } from "./VizNote";
import RowsView from "./RowsViews";
import { HeatmapChart, PolarAreaChart, RadarChart, WordCloud } from "./PolarCharts";
import ScatterChart from "./ScatterChart";
import SingleValue from "./SingleValue";
import XYChart from "./XYChart";

/** The data of the charts, values and tables; maps, graphs and flows are `StudioViz`'s. */
type ChartData = Extract<WidgetData, { status: "ok" }> & WidgetChartData;

function isEmpty(data: ChartData): boolean {
  switch (data.shape) {
    case "number":
      return false;
    case "series":
      return data.data.series.every((s) => s.points.length === 0);
    case "distribution":
      return data.data.items.length === 0;
    case "rows":
      return data.data.rows.length === 0;
    case "text":
      return data.data.text.trim() === "";
    case "outcomes":
      // A validation view says itself what it shows with no outcome: a sample, a request, every label untested.
      return false;
  }
}

/** An answer in words, kept as written: paragraphs, no markup run. */
function TextView({ text, height }: { text: string; height: number }) {
  return (
    <div className="overflow-auto pr-1" style={{ maxHeight: height }}>
      <div className="flex flex-col gap-2 whitespace-pre-wrap break-words content-base leading-relaxed text-default-primary">
        {text.split(/\n{2,}/).map((paragraph, index) => (
          <p key={index}>{paragraph}</p>
        ))}
      </div>
    </div>
  );
}

const VALUE_VIEWS = new Set<PlatformView>(["number", "icon", "radial", "filler", "marker"]);

/** The renderer for this shape and view. */
function Drawing({ view, ...props }: ChartProps & { view: PlatformView }) {
  if (props.shape === "outcomes") {
    // Outcomes drawn another way take the console, the views' first summary of them.
    return <OutcomeViews data={props.data as WidgetOutcomesData} view={isOutcomeView(view) ? view : "exposureconsole"} height={props.height} options={props.scope.options} />;
  }
  if (view === "radar") return <RadarChart {...props} />;
  if (view === "polar") return <PolarAreaChart {...props} />;
  if (view === "wordcloud") return <WordCloud {...props} />;
  if (view === "heatmap") return <HeatmapChart {...props} />;
  if (VALUE_VIEWS.has(view) || props.shape === "number") {
    return <SingleValue {...props} view={VALUE_VIEWS.has(view) ? (view as "number") : "number"} />;
  }
  if (view === "scatter" || view === "bubble" || view === "punchcard") return <ScatterChart {...props} view={view} />;
  if (view === "donut") return <PieChart {...props} />;
  if (props.shape === "rows") {
    return view === "list" ? <RowsView data={props.data as WidgetRowsData} options={props.scope.options} height={props.height} /> : <DataTable {...props} />;
  }
  if (props.shape === "distribution" && view === "distribution") {
    return <DistributionList data={props.data as WidgetDistributionData} options={props.scope.options} />;
  }
  return <XYChart {...props} view={view} />;
}

/** A greyed sample in place of data with nothing to show, marked so; nothing in it reacts to the pointer. */
function Sample({ variant, children }: { variant: "full" | "subtle"; children: ReactNode }) {
  return (
    <div className="relative" data-sample={variant}>
      <span className="absolute right-0 top-0 z-10">
        <SampleBadge />
      </span>
      <div
        aria-hidden="true"
        className="pointer-events-none select-none"
        style={variant === "full" ? { filter: "grayscale(1)", opacity: 0.45 } : { filter: "grayscale(0.6)", opacity: 0.8 }}
      >
        {children}
      </div>
    </div>
  );
}

export default function PlatformChart({
  data,
  view,
  height,
  options = {},
  context = {},
  annotation,
  onSelect,
  onBrush,
}: {
  data: ChartData;
  view: DataView;
  /** The chart's height in pixels (`grid.ts` `chartHeight`). */
  height: number;
  /** The visualization's options, the definition's defaults merged in. */
  options?: Record<string, unknown>;
  /** The visualization's `context`: the configs its dynamic options name. */
  context?: Record<string, unknown>;
  /** The data of its annotation source, when it has one and it answered. */
  annotation?: Extract<WidgetData, { status: "ok" }>;
} & ChartEvents) {
  const { t_i18n } = useFormatter();
  // A widget whose look asks for one draws a greyed sample while it has nothing to show (only zeros included).
  const sample = readLook(options).emptySample;
  if (sample && (data.shape === "series" || data.shape === "distribution") && isAllEmpty(data.shape, data.data)) {
    const drawn = sampleFor(sample, data.shape, data.data);
    const scope = { primary: frameOf(drawn.shape, drawn.data), annotation: undefined, options: { ...options, ...drawn.options }, context } satisfies DosScope;
    return (
      <Sample variant={drawn.variant}>
        <Drawing shape={drawn.shape} data={drawn.data} scope={scope} height={height} view={view === "text" ? "list" : view} />
      </Sample>
    );
  }
  if (isEmpty(data)) {
    const empty = emptyNote(options);
    if (empty.inbox) {
      return (
        <VizNote height={height} inbox>
          {t_i18n(empty.message)}
        </VizNote>
      );
    }
    return (
      <div
        className={cn("flex items-center justify-center content-compact", empty.strong ? "text-default-primary" : "text-default-secondary")}
        style={empty.fill ? { height } : { minHeight: 80 }}
      >
        {t_i18n(empty.message)}
      </div>
    );
  }
  if (data.shape === "text") return <TextView text={data.data.text} height={height} />;
  // A text view on a shape that is not text draws it as a list.
  const drawnView: PlatformView = view === "text" ? "list" : view;
  const scope: ChartProps["scope"] = {
    primary: frameOf(data.shape, data.data),
    annotation: annotation ? frameOf(annotation.shape, annotation.data) : undefined,
    options,
    context,
  } satisfies DosScope;
  return (
    <ChartEventsContext.Provider value={{ onSelect, onBrush }}>
      <Drawing shape={data.shape} data={data.data} scope={scope} height={height} view={drawnView} />
    </ChartEventsContext.Provider>
  );
}
