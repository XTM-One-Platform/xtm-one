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
 * How a custom dashboard page reads its data and its files.
 *
 * - `session` (the app): the signed-in person's routes, with their own access.
 * - `render` (the export's static render page, `DashboardStaticRender`): the
 *   render routes, with the confined render token the server minted for the
 *   person who asked for the export - never their session.
 * - `snapshot` (a published link, `DashboardPublic`): the data stored when the
 *   link's snapshot was taken, read with its publisher's access on the
 *   server; nothing is read live and nothing is asked.
 *
 * The data hooks of `useCustomDashboardApi` and the canvases' files read it
 * from `DashboardAccessContext`; the app provides nothing, so it is `session`.
 */
import { createContext, useContext } from "react";

import { api } from "@/lib/api";
import type { WidgetData } from "@/types";

interface DashboardSnapshot {
  /** Each visualization's data, by its id. */
  widgets: Record<string, WidgetData>;
  /** Each data source's data, by its id. */
  sources: Record<string, WidgetData>;
}

export type DashboardAccess =
  | { kind: "session" }
  /** `entityNames`: the entity inputs' starting entities named by the server, by id - this page cannot search. */
  | { kind: "render"; token: string; entityNames?: Record<string, string> }
  | { kind: "snapshot"; publicToken: string; snapshot: DashboardSnapshot };

const SESSION_ACCESS: DashboardAccess = { kind: "session" };

export const DashboardAccessContext = createContext<DashboardAccess>(SESSION_ACCESS);

export const useDashboardAccess = (): DashboardAccess => useContext(DashboardAccessContext);

/** What a snapshot holds for an element it did not record: it shows as having no data. */
const NOT_IN_SNAPSHOT: WidgetData = { status: "error", code: "no_result", open_url: null };

/** Read a visualization's (`widgets`) or a data source's (`data-sources`) data as the page's access allows. */
/** Until when each dashboard's reads ask their platforms again (`fresh`): a few seconds after its Refresh. */
const freshUntil = new Map<string, number>();
const FRESH_WINDOW_MS = 10_000;

/** The next reads of a dashboard (those its Refresh starts) ask the platforms again rather than the answer kept a minute. */
export function markDashboardFresh(dashboardId: string, now = Date.now()) {
  freshUntil.set(dashboardId, now + FRESH_WINDOW_MS);
}

export function readDashboardData(
  access: DashboardAccess,
  dashboardId: string,
  kind: "widgets" | "data-sources",
  id: string,
  body: Record<string, unknown>,
): Promise<WidgetData> {
  const path = `${kind}/${encodeURIComponent(id)}/data`;
  switch (access.kind) {
    case "session":
      return api.post<WidgetData>(
        `/custom-dashboards/${dashboardId}/${path}`,
        (freshUntil.get(dashboardId) ?? 0) > Date.now() ? { ...body, fresh: true } : body,
      );
    case "render":
      return api.postAs<WidgetData>(`/custom-dashboards/${dashboardId}/render/${path}`, access.token, body);
    case "snapshot": {
      const stored = kind === "widgets" ? access.snapshot.widgets[id] : access.snapshot.sources[id];
      return Promise.resolve(stored ?? NOT_IN_SNAPSHOT);
    }
  }
}

/** Where a file a canvas draws is read from (fetched with `useAuthenticatedSrc` in the app). */
export function dashboardAssetUrl(access: DashboardAccess, dashboardId: string, fileId: string): string {
  switch (access.kind) {
    case "session":
      return `/api/v1/custom-dashboards/${dashboardId}/assets/${fileId}`;
    case "render":
      return `/api/v1/custom-dashboards/${dashboardId}/render/assets/${fileId}?render_token=${encodeURIComponent(access.token)}`;
    case "snapshot":
      return `/api/v1/public/dashboards/${encodeURIComponent(access.publicToken)}/assets/${fileId}`;
  }
}
