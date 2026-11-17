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
 * Static render page behind the report PDF export — `/reports/:id/static`.
 * Not a user-facing page.
 *
 * The platform's headless Chromium (`backend/app/services/report_export.py`)
 * loads this route with a **render token** in the query string and nothing
 * else: no session, no cookies. It draws the report exactly as the viewer's
 * print route does — `ReportView` in its `print` variant, every annex open —
 * and the exporter prints it with `page.pdf()`, paginated by the report's own
 * `@page` rule (`report.css`), so the PDF is the document the print dialog
 * would produce.
 *
 * Protocol with the exporter (on `document.documentElement.dataset`):
 *   - `reportReady = "true"` once the report is rendered and its fonts are ready;
 *   - `reportError = <message>` when the report cannot be loaded.
 */
import { useCallback, useEffect, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";

import { ReportView } from "@/components/report/ReportView";
import { API_BASE } from "@/lib/api";
import type { Report } from "@/types";

const RENDER_TOKEN_PARAM = "render_token";

type LoadState =
  | { status: "loading" }
  | { status: "ready"; report: Report }
  | { status: "error"; message: string };

export default function ReportStaticRender() {
  const { id } = useParams<{ id: string }>();
  const [searchParams] = useSearchParams();
  const token = searchParams.get(RENDER_TOKEN_PARAM) ?? "";
  const [state, setState] = useState<LoadState>({ status: "loading" });

  // Plain fetch on purpose: `api.ts` attaches the session JWT (there is none
  // here) and redirects to the login page on 401, which would hang the export.
  useEffect(() => {
    if (!id || !token) {
      setState({ status: "error", message: "Missing report id or render token." });
      return;
    }
    let cancelled = false;
    const url = `${API_BASE}/reports/${encodeURIComponent(id)}/render-spec?${RENDER_TOKEN_PARAM}=${encodeURIComponent(token)}`;
    fetch(url, { credentials: "omit" })
      .then(async (res) => {
        if (!res.ok) throw new Error(`The report could not be loaded (HTTP ${res.status}).`);
        return (await res.json()) as Report;
      })
      .then((report) => {
        if (!cancelled) setState({ status: "ready", report });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setState({
          status: "error",
          message: error instanceof Error ? error.message : String(error),
        });
      });
    return () => {
      cancelled = true;
    };
  }, [id, token]);

  // Signal failure to the exporter. Cleared on unmount so a hot-reload in dev
  // does not leave a stale flag behind.
  useEffect(() => {
    if (state.status !== "error") return;
    document.documentElement.dataset.reportError = state.message;
    return () => {
      delete document.documentElement.dataset.reportError;
    };
  }, [state]);

  // The PDF's title metadata is the document title.
  const title = state.status === "ready" ? state.report.title : "";
  useEffect(() => {
    if (title) document.title = title;
  }, [title]);

  // Ready once the fonts have loaded and the laid-out report has painted: a
  // report carries no image and no lazily drawn figure.
  const signalReady = useCallback((root: HTMLDivElement | null) => {
    if (!root) return;
    let cancelled = false;
    const fontsReady =
      typeof document.fonts?.ready?.then === "function" ? document.fonts.ready : Promise.resolve();
    void fontsReady.then(() => {
      window.requestAnimationFrame(() =>
        window.requestAnimationFrame(() => {
          if (!cancelled) document.documentElement.dataset.reportReady = "true";
        }),
      );
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (state.status !== "ready") {
    // Nothing to paint: the exporter reads `reportError` (set above) or times out.
    return null;
  }

  return (
    <div ref={signalReady}>
      <ReportView report={state.report} variant="print" />
    </div>
  );
}
