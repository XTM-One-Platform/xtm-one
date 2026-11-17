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
 * The chat surface for a `[[REPORT:uuid]]` marker.
 *
 * Renders the report inline, scrollable inside the message flow, with a toolbar
 * for exporting it (`ReportExportMenu`: the server-side PDF, or the
 * browser's own print of the `@media print` block in `report.css`) and for
 * opening the standalone viewer.
 *
 * The document is tall by nature, so the embedded view is height-capped and
 * scrolls: a 14-block executive brief would otherwise push the rest of the
 * conversation off the screen. The standalone viewer is the full-height read.
 */
import { useCallback, useRef, useState } from "react";
import {
  ExternalLink,
  FileText,
  Maximize2,
} from "lucide-react";

import { UnavailableCard } from "@/components/chat/UnavailableCard";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useReport } from "@/hooks/api/useReportApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { withBasePath } from "@/lib/base-path";

import { ReportExportMenu } from "./ReportExportMenu";
import { ReportView } from "./ReportView";
import { Spinner } from "@filigran/design-system";

/** Shared shell so the loading, error and loaded states share one frame. */
function CardFrame({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "my-2 overflow-hidden rounded-sm border border-accent/25 layer-1 bg-elevation-default",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function ReportCard({ reportId }: { reportId: string }) {
  const { t_i18n } = useFormatter();
  const { data, isLoading, isError } = useReport(reportId);
  const containerRef = useRef<HTMLDivElement>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);

  const toggleFullscreen = useCallback(() => {
    const element = containerRef.current;
    if (!element) return;
    if (document.fullscreenElement) {
      void document.exitFullscreen().catch(() => setIsFullscreen(false));
      setIsFullscreen(false);
      return;
    }
    // The Promise is stored before `.then` because optional chaining covers only
    // the CALL: where `requestFullscreen` is undefined, `?.()` yields undefined
    // and chaining onto it throws. And the API rejects on its own whenever the
    // gesture is not user-activated or the permission is denied, so an unhandled
    // rejection would surface as a console error every time a browser says no.
    const request = element.requestFullscreen?.();
    if (!request) return;
    request.then(() => setIsFullscreen(true)).catch(() => setIsFullscreen(false));
  }, []);

  if (isLoading) {
    return (
      <CardFrame className="flex items-center gap-2 px-3 py-2.5 text-[13px] text-default-secondary">
        <Spinner size="sm" tone="inherit" className="text-accent" />
        {t_i18n("Loading report…")}
      </CardFrame>
    );
  }

  if (isError || !data) {
    return <UnavailableCard message={t_i18n("This report is no longer available.")} />;
  }

  const viewerUrl = `/reports/${data.id}`;

  return (
    <CardFrame>
      <div className="flex items-center justify-between gap-3 border-b border-elevation-subtle-soft px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <FileText className="h-4 w-4 shrink-0 text-accent" />
          <span className="truncate text-[13px] font-medium text-default-primary">
            {data.title}
          </span>
          <span className="shrink-0 text-[11px] text-default-secondary">
            {t_i18n("{count} sections", { count: data.block_count })}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Tip content={t_i18n("Read fullscreen")}>
            <Button
              variant="ghost"
              size="sm"
              onClick={toggleFullscreen}
              aria-label={t_i18n("Read fullscreen")}
            >
              <Maximize2 className="h-3.5 w-3.5" />
            </Button>
          </Tip>
          <ReportExportMenu reportId={data.id} compact className="h-7 w-7" />
          <Tip content={t_i18n("Open in a new tab")}>
            <Button variant="ghost" size="sm" asChild aria-label={t_i18n("Open in a new tab")}>
              <a href={withBasePath(viewerUrl)} target="_blank" rel="noreferrer">
                <ExternalLink className="h-3.5 w-3.5" />
              </a>
            </Button>
          </Tip>
        </div>
      </div>
      <div
        ref={containerRef}
        className={cn(
          "w-full overflow-y-auto bg-elevation-default",
          isFullscreen ? "h-screen" : "max-h-[520px]",
        )}
      >
        <ReportView report={data} variant="embedded" />
      </div>
    </CardFrame>
  );
}
