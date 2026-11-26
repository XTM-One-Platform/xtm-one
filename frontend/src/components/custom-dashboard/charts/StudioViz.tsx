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
 * The drawing of the maps, graphs, flows and trellises of a dashboard.
 *
 * Loaded lazily, like `PlatformChart`, which keeps the charts, values and
 * tables; the map (with its atlas) and the graphs (with React Flow) are lazy
 * again inside it, so a sankey downloads neither. Which data each view reads
 * is in `viz/shapes.ts`, where each view lays itself out in `viz/*.ts`; what
 * does not fit the view says so in one sentence.
 *
 * Every string is the server's closed data, drawn as text (React escapes
 * it); an uploaded SVG is redrawn from its shapes alone (`SvgChoropleth`).
 */
import { lazy, Suspense, useRef, type ReactNode } from "react";

import { useMeasuredBox, type Box } from "@/components/presentation/measured-box";
import { Skeleton } from "@/components/ui/skeleton";
import { useFormatter } from "@/i18n";
import type { WidgetData } from "@/types";

import type { PlatformView } from "../spec";
import { readTimelineStyle } from "../viz/alternate";
import { TRELLIS_VIEWS } from "../viz/route";
import { distributionToTree, rowsToEvents, rowsToGraph, rowsToStages } from "../viz/shapes";
import { trellisOptions, trellisPanels } from "../viz/trellis";
import AlternateTimeline from "./AlternateTimeline";
import { ParallelViz } from "./ParallelViz";
import { SankeyViz } from "./SankeyViz";
import { SvgChoropleth } from "./SvgChoropleth";
import { TimelineViz } from "./TimelineViz";
import { TreemapViz } from "./TreemapViz";
import { TrellisViz } from "./TrellisViz";
import { emptyNote } from "./look";
import { VizNote } from "./VizNote";

const MapViz = lazy(() => import("./MapViz"));
const GraphViz = lazy(() => import("./GraphViz"));
const AttackFlow = lazy(() => import("./AttackFlow"));

type OkData = Extract<WidgetData, { status: "ok" }>;

/** What a view says of data it cannot draw. @i18n-keys */
const MISFITS = {
  map: "A map draws places: points with a latitude and a longitude, or regions by code.",
  choroplethsvg: "A choropleth on an SVG draws regions by code on the SVG of its options.",
  graph: "A graph draws nodes and edges, or a table's source and target columns.",
  parallel: "Parallel coordinates draw a table, one axis per column.",
  timeline: "A timeline draws events, or a table with a date column.",
  treemap: "A treemap draws values on a hierarchy, or a distribution.",
  trellis: "This data cannot be split into panels.",
  view: "This view cannot draw this data.",
} as const;

/** A box `height` px tall, as wide as its cell, measured; its drawing waits for a size. */
function Measured({ height, children }: { height: number; children: (box: Box) => ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const box = useMeasuredBox(ref);
  return (
    <div ref={ref} className="relative w-full" style={{ height }}>
      {box ? children(box) : null}
    </div>
  );
}

function isEmpty(data: OkData): boolean {
  switch (data.shape) {
    case "number":
      return false;
    case "series":
      return data.data.series.every((s) => s.points.length === 0);
    case "distribution":
      return data.data.items.length === 0;
    case "rows":
      return data.data.rows.length === 0;
    case "geo":
      return data.data.points.length === 0 && data.data.regions.length === 0;
    case "graph":
      return data.data.edges.length === 0 && data.data.nodes.length === 0;
    case "events":
      return data.data.events.length === 0;
    case "tree":
    case "outcomes":
      return data.data.items.length === 0;
    case "text":
      return data.data.text.trim() === "";
  }
}

export default function StudioViz({
  data,
  view,
  height,
  options,
}: {
  data: OkData;
  view: PlatformView | undefined;
  height: number;
  options: Record<string, unknown>;
}) {
  const { t_i18n } = useFormatter();
  if (isEmpty(data)) {
    const empty = emptyNote(options);
    return (
      <VizNote height={height} fill={empty.fill} inbox={empty.inbox} strong={empty.strong}>
        {t_i18n(empty.message)}
      </VizNote>
    );
  }
  const misfit = (key: keyof typeof MISFITS) => <VizNote height={height}>{t_i18n(MISFITS[key])}</VizNote>;
  const lazyFallback = (
    <div style={{ height }}>
      <Skeleton className="h-full w-full" />
    </div>
  );

  const trellis = trellisOptions(options);
  if (trellis && view && (TRELLIS_VIEWS as readonly string[]).includes(view)) {
    const panels = trellisPanels(data, trellis);
    if (!panels) return misfit("trellis");
    return <TrellisViz trellis={panels} columns={trellis.columns} view={view} height={height} />;
  }

  switch (view) {
    case "map":
      if (data.shape !== "geo") return misfit("map");
      return (
        <Suspense fallback={lazyFallback}>
          <Measured height={height}>{(box) => <MapViz data={data.data} options={options} width={box.width} height={box.height} />}</Measured>
        </Suspense>
      );
    case "choroplethsvg":
      if (data.shape !== "geo" || typeof options.svg !== "string") return misfit("choroplethsvg");
      return <SvgChoropleth data={data.data} svg={options.svg} height={height} />;
    case "linkgraph":
    case "networkgraph": {
      const graph =
        data.shape === "graph"
          ? data.data
          : data.shape === "rows"
            ? view === "linkgraph"
              ? rowsToStages(data.data, options)
              : rowsToGraph(data.data, options)
            : null;
      if (!graph || graph.edges.length === 0) return misfit("graph");
      return (
        <Suspense fallback={lazyFallback}>
          <Measured height={height}>{(box) => <GraphViz graph={graph} kind={view === "linkgraph" ? "link" : "network"} width={box.width} height={box.height} />}</Measured>
        </Suspense>
      );
    }
    case "attackflow":
      if (data.shape !== "graph" || data.data.nodes.length === 0) return misfit("graph");
      return (
        <Suspense fallback={lazyFallback}>
          <Measured height={height}>{(box) => <AttackFlow graph={data.data} width={box.width} height={box.height} />}</Measured>
        </Suspense>
      );
    case "sankey": {
      const graph = data.shape === "graph" ? data.data : data.shape === "rows" ? rowsToGraph(data.data, options) : null;
      if (!graph || graph.edges.length === 0) return misfit("graph");
      return <Measured height={height}>{(box) => <SankeyViz graph={graph} width={box.width} height={box.height} />}</Measured>;
    }
    case "parallel":
      if (data.shape !== "rows") return misfit("parallel");
      return <Measured height={height}>{(box) => <ParallelViz data={data.data} fields={options.fields} width={box.width} height={box.height} />}</Measured>;
    case "timeline": {
      const events = data.shape === "events" ? data.data : data.shape === "rows" ? rowsToEvents(data.data, options) : null;
      if (!events || events.events.length === 0) return misfit("timeline");
      if (readTimelineStyle(options) === "alternate") return <AlternateTimeline data={events} height={height} />;
      return <Measured height={height}>{(box) => <TimelineViz data={events} width={box.width} height={box.height} />}</Measured>;
    }
    case "treemap": {
      const tree = data.shape === "tree" ? data.data : data.shape === "distribution" ? distributionToTree(data.data) : null;
      if (!tree || tree.items.length === 0) return misfit("treemap");
      return <Measured height={height}>{(box) => <TreemapViz data={tree} width={box.width} height={box.height} options={options} />}</Measured>;
    }
    default:
      return misfit("view");
  }
}
