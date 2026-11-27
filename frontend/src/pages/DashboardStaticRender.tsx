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
 * Static render page behind the dashboard PDF and PNG export -
 * `/dashboards/:id/static`. Not a user-facing page.
 *
 * The platform's headless Chromium (`backend/app/services/dashboard_export.py`)
 * loads this route with a **render token** in the query string and nothing
 * else: no session, no cookies. It draws the dashboard as its viewers see it,
 * with no app chrome: the tabs named by `?tab=` (all of them by default), one
 * under the other, each a page of the PDF (`data-export-page`) - or, with
 * `?viz=`, that one visualization alone. Every source is read through the
 * render routes with the token (`DashboardAccessContext`, kind `render`): as
 * the person who asked for the export reads it.
 *
 * Protocol with the exporter (on `document.documentElement.dataset`):
 *   - `dashboardReady = "true"` once no read is in flight for `SETTLE_MS`
 *     (the charts have drawn their data and finished animating) and the fonts
 *     are ready;
 *   - `dashboardError = <message>` when the dashboard cannot be loaded.
 *
 * Its entity inputs show the entities they start on by the names the render
 * route gives (`entity_names`): the page has no session to search with.
 */
import { useEffect, useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { useIsFetching } from "@tanstack/react-query";

import { CustomDashboardView } from "@/components/custom-dashboard/CustomDashboardView";
import { normalizeDashboard, type DashboardTab, type NormalizedDashboard } from "@/components/custom-dashboard/spec";
import { DashboardAccessContext, type DashboardAccess } from "@/hooks/api/dashboard-access";
import { API_BASE } from "@/lib/api";
import type { CustomDashboard } from "@/types";

const RENDER_TOKEN_PARAM = "render_token";

/** How long no read may be in flight before the page is final: a chart animates its data in. */
const SETTLE_MS = 2000;

/** The width one visualization is drawn at, alone. */
const VISUALIZATION_WIDTH = 960;

type LoadState =
  | { status: "loading" }
  | { status: "ready"; dashboard: CustomDashboard & { entity_names?: Record<string, string> } }
  | { status: "error"; message: string };

/** The dashboard reduced to one visualization, alone on a grid tab of its own. */
function aloneOf(dashboard: NormalizedDashboard, vizId: string): NormalizedDashboard | null {
  const widget = dashboard.tabs.flatMap((tab) => tab.widgets).find((candidate) => candidate.id === vizId);
  if (!widget) return null;
  const rows = widget.layout?.h ?? 8;
  const tab: DashboardTab = { id: "export", label: "", type: "grid", widgets: [{ ...widget, layout: { x: 0, y: 0, w: 12, h: rows } }] };
  return {
    ...dashboard,
    subtitle: undefined,
    tabs: [tab],
    widgets: tab.widgets,
    showTabBar: false,
    inputs: { ...dashboard.inputs, globalInputs: [] },
  };
}

/** Ready once nothing has been read for `SETTLE_MS` and the fonts are in. */
function useSettled(active: boolean): void {
  const fetching = useIsFetching();
  useEffect(() => {
    if (!active || fetching > 0) return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      const fonts = typeof document.fonts?.ready?.then === "function" ? document.fonts.ready : Promise.resolve();
      void fonts.then(() =>
        window.requestAnimationFrame(() => {
          if (!cancelled) document.documentElement.dataset.dashboardReady = "true";
        }),
      );
    }, SETTLE_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [active, fetching]);
}

export default function DashboardStaticRender() {
  const { id = "" } = useParams<{ id: string }>();
  const [searchParams] = useSearchParams();
  const token = searchParams.get(RENDER_TOKEN_PARAM) ?? "";
  const tabIds = searchParams.getAll("tab");
  const vizId = searchParams.get("viz");
  const [state, setState] = useState<LoadState>({ status: "loading" });

  // Plain fetch on purpose: `api.ts` attaches the session JWT (there is none
  // here) and redirects to the login page on 401, which would hang the export.
  useEffect(() => {
    if (!id || !token) {
      setState({ status: "error", message: "Missing dashboard id or render token." });
      return;
    }
    let cancelled = false;
    fetch(`${API_BASE}/custom-dashboards/${encodeURIComponent(id)}/render?${RENDER_TOKEN_PARAM}=${encodeURIComponent(token)}`, {
      credentials: "omit",
    })
      .then(async (res) => {
        if (!res.ok) throw new Error(`The dashboard could not be loaded (HTTP ${res.status}).`);
        return (await res.json()) as CustomDashboard & { entity_names?: Record<string, string> };
      })
      .then((dashboard) => {
        if (!cancelled) setState({ status: "ready", dashboard });
      })
      .catch((error: unknown) => {
        if (!cancelled) setState({ status: "error", message: error instanceof Error ? error.message : String(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [id, token]);

  const normalized = useMemo(() => (state.status === "ready" ? normalizeDashboard(state.dashboard.spec) : null), [state]);
  const alone = useMemo(() => (normalized && vizId ? aloneOf(normalized, vizId) : null), [normalized, vizId]);
  const tabKey = tabIds.join("\n");
  const tabs = useMemo(() => {
    if (!normalized) return [];
    const wanted = tabKey ? tabKey.split("\n") : [];
    return wanted.length ? normalized.tabs.filter((tab) => wanted.includes(tab.id)) : normalized.tabs;
  }, [normalized, tabKey]);
  const failure = state.status === "error" ? state.message : normalized && vizId && !alone ? "No such visualization." : null;

  useEffect(() => {
    if (!failure) return;
    document.documentElement.dataset.dashboardError = failure;
    return () => {
      delete document.documentElement.dataset.dashboardError;
    };
  }, [failure]);

  // The PDF's title metadata is the document title.
  const title = state.status === "ready" ? state.dashboard.title : "";
  useEffect(() => {
    if (title) document.title = title;
  }, [title]);

  // The entity inputs' entities, named by the server: this page has no session to search a platform with.
  const entityNames = state.status === "ready" ? state.dashboard.entity_names : undefined;
  const access = useMemo<DashboardAccess>(() => ({ kind: "render", token, entityNames }), [token, entityNames]);
  useSettled(!!normalized && !failure);

  if (!normalized || failure) return null;
  return (
    <DashboardAccessContext.Provider value={access}>
      {alone ? (
        <div data-export-root className="bg-elevation-default p-4" style={{ width: VISUALIZATION_WIDTH }}>
          <CustomDashboardView dashboard={alone} dashboardId={id} />
        </div>
      ) : (
        <div data-export-root className="bg-elevation-default">
          {tabs.map((tab) => (
            <section key={tab.id} data-export-page className="break-after-page p-6">
              <h1 className="mb-1 text-xl font-semibold text-default-primary">{normalized.title}</h1>
              {normalized.tabs.length > 1 && <h2 className="mb-4 content-base text-default-secondary">{tab.label}</h2>}
              <CustomDashboardView dashboard={{ ...normalized, showTabBar: false }} dashboardId={id} tabId={tab.id} />
            </section>
          ))}
        </div>
      )}
    </DashboardAccessContext.Provider>
  );
}
