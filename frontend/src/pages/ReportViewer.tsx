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
 * Standalone report viewer — `/reports/:id`.
 *
 * Rendered OUTSIDE the app shell (no sidebar, no header) so a report can be read
 * or printed without the product chrome, and so external channels have somewhere
 * to point (the channel rewriter turns `[[REPORT:…]]` markers into links here).
 *
 * With `?print` in the URL every collapsible annex starts expanded and the browser
 * print dialog opens on its own: the `@media print` block in `report.css` does
 * the pagination, so the printed document looks exactly like the one on screen.
 * The Export menu in the corner (`ReportExportMenu`, never printed) offers the
 * same document as a PDF the platform renders server-side, or this
 * print route.
 */
import { useEffect } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { AlertTriangle } from "lucide-react";

import { ReportExportMenu } from "@/components/report/ReportExportMenu";
import { ReportView } from "@/components/report/ReportView";
import { useReport } from "@/hooks/api/useReportApi";
import { useFormatter } from "@/i18n";
import { Spinner } from "@filigran/design-system";

/** Give the layout a beat to settle before the print dialog captures it. */
const PRINT_DELAY_MS = 400;

export default function ReportViewer() {
  const { id } = useParams<{ id: string }>();
  const [searchParams] = useSearchParams();
  const { t_i18n } = useFormatter();
  const { data, isLoading, isError } = useReport(id);
  const isPrinting = searchParams.has("print");

  // The report title is the tab title: a printed report is usually opened in its
  // own tab, where the tab label is the only thing identifying it.
  useEffect(() => {
    if (!data?.title) return;
    const previous = document.title;
    document.title = data.title;
    return () => {
      document.title = previous;
    };
  }, [data?.title]);

  // Print only once the document is actually rendered — printing during the
  // loading state would capture a spinner. The delay lets fonts and the forced
  // annex expansion land before the dialog snapshots the layout.
  useEffect(() => {
    if (!isPrinting || !data) return;
    const timer = window.setTimeout(() => window.print(), PRINT_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [isPrinting, data]);

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center bg-elevation-default">
        <Spinner size="lg" tone="inherit" className="text-accent" label={t_i18n("Loading...")} />
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-3 bg-elevation-default px-6 text-center">
        <AlertTriangle className="h-6 w-6 text-feedback-warning-primary" />
        <p className="content-base text-default-secondary">
          {t_i18n("This report is no longer available.")}
        </p>
      </div>
    );
  }

  return (
    // `min-h-screen`, never `h-screen`: a report is as tall as its content, and a
    // viewport-height wrapper would clip the print output to page one.
    <div className="min-h-screen w-full bg-elevation-default">
      {!isPrinting && (
        <div className="fixed right-4 top-4 z-10 rounded-sm border border-elevation-subtle layer-1 bg-elevation-default backdrop-blur-sm print:hidden">
          <ReportExportMenu reportId={data.id} />
        </div>
      )}
      <ReportView report={data} variant={isPrinting ? "print" : "standalone"} />
    </div>
  );
}
