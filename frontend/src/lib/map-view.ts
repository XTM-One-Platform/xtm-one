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
 * The Agentic Flow map's views as path routes (Sam, 8 Nov 2026, #10203 S2):
 * `/flows/map` is the handover graph, `/flows/map/schedule` the schedule of
 * the same agents. The query keeps only the map's state (`?vis=`, `?focus=`,
 * `?agent=`, the layout and filters). The former `?view=` query is not read and
 * an unknown view segment is not found: no redirect (#797).
 */

export type MapView = "graph" | "schedule";

/** The path of a map view: the graph is the page with none. */
export function mapViewPath(view: MapView): string {
  return view === "graph" ? "/flows/map" : `/flows/map/${view}`;
}

/** The view a `:mapView` path segment names; none or an unknown one is the graph. */
export function readMapView(segment: string | undefined): MapView {
  return segment === "schedule" ? "schedule" : "graph";
}

/** Whether a `:mapView` segment names a view (none is the graph); an unknown one is not found. */
export function isKnownMapView(segment: string | undefined): boolean {
  return segment === undefined || segment === "schedule";
}

