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
 * Which drawing a live widget's data goes to: the charts, values and tables
 * (`PlatformChart`), or the maps, graphs, flows and trellises (`StudioViz`).
 *
 * Pure, type-only imports: runs on `node --test`.
 */
import type { WidgetChartData, WidgetData } from "@/types";

import { trellisOptions } from "./trellis.ts";

/** The views `StudioViz` draws. */
const STUDIO_VIEWS = ["map", "choroplethsvg", "linkgraph", "networkgraph", "sankey", "parallel", "timeline", "treemap", "attackflow"] as const;
type StudioView = (typeof STUDIO_VIEWS)[number];

/** The chart views a trellis splits into small multiples. */
export const TRELLIS_VIEWS = ["bar", "hbar", "line", "area", "donut", "distribution"] as const;

type OkData = Extract<WidgetData, { status: "ok" }>;

const isStudioView = (view: string | undefined): view is StudioView =>
  view !== undefined && (STUDIO_VIEWS as readonly string[]).includes(view);

const CHART_SHAPES = ["number", "series", "distribution", "rows", "text", "outcomes"];

/** The data `PlatformChart` draws as it is, with no trellis asked. */
export function drawnAsChart(data: OkData, view: string | undefined, options: Record<string, unknown>): data is OkData & WidgetChartData {
  if (!CHART_SHAPES.includes(data.shape) || isStudioView(view)) return false;
  return !(trellisOptions(options) && (TRELLIS_VIEWS as readonly string[]).includes(view ?? ""));
}
