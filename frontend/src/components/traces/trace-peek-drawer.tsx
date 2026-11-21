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

import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { Link } from "react-router-dom";
import { ExternalLink, Scan } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { MaskedCost } from "@/components/ui/masked-cost";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { PageSkeleton } from "@/components/ui/skeleton";
import { TraceExplorer } from "@/components/traces/trace-explorer";
import { useTrace, useTraceSpans } from "@/hooks/api/useTraceApi";
import { useCostDataAvailability } from "@/hooks/useCostDataAvailability";
import { formatDuration, resolveProviderFromMetadata, traceStatusLabelKey } from "@/lib/trace-utils";
import { exactCompactCost, exactMetricValue, formatCompactCost, formatCompactNumber } from "@/lib/utils";
import { useFormatter } from "@/i18n";

interface TracePeekDrawerProps {
  traceId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const DEFAULT_DRAWER_WIDTH = 1380;
const MIN_DRAWER_WIDTH = 760;
const DRAWER_SIDE_GUTTER = 24;

export function TracePeekDrawer({ traceId, open, onOpenChange }: TracePeekDrawerProps) {
  const { t_i18n } = useFormatter();
  const costDataAvailable = useCostDataAvailability();
  const clampDrawerWidth = useCallback((width: number) => {
    if (typeof window === "undefined") return Math.max(width, MIN_DRAWER_WIDTH);
    const maxWidth = Math.max(MIN_DRAWER_WIDTH, window.innerWidth - DRAWER_SIDE_GUTTER);
    return Math.min(Math.max(width, MIN_DRAWER_WIDTH), maxWidth);
  }, []);
  const [drawerWidth, setDrawerWidth] = useState(() => (
    typeof window === "undefined"
      ? DEFAULT_DRAWER_WIDTH
      : Math.min(DEFAULT_DRAWER_WIDTH, Math.max(MIN_DRAWER_WIDTH, window.innerWidth - DRAWER_SIDE_GUTTER))
  ));
  const [isResizing, setIsResizing] = useState(false);
  const dragStartXRef = useRef(0);
  const dragStartWidthRef = useRef(drawerWidth);

  const stopResizing = useCallback(() => {
    setIsResizing(false);
    document.body.style.removeProperty("cursor");
    document.body.style.removeProperty("user-select");
  }, []);

  const onResizePointerMove = useCallback((event: PointerEvent) => {
    const delta = dragStartXRef.current - event.clientX;
    setDrawerWidth(clampDrawerWidth(dragStartWidthRef.current + delta));
  }, [clampDrawerWidth]);

  const onResizePointerUp = useCallback(() => {
    stopResizing();
    window.removeEventListener("pointermove", onResizePointerMove);
    window.removeEventListener("pointerup", onResizePointerUp);
  }, [onResizePointerMove, stopResizing]);

  const onResizePointerDown = useCallback((event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault();
    dragStartXRef.current = event.clientX;
    dragStartWidthRef.current = drawerWidth;
    setIsResizing(true);
    document.body.style.setProperty("cursor", "col-resize");
    document.body.style.setProperty("user-select", "none");
    window.addEventListener("pointermove", onResizePointerMove);
    window.addEventListener("pointerup", onResizePointerUp);
  }, [drawerWidth, onResizePointerMove, onResizePointerUp]);

  useEffect(() => {
    if (!open) return;
    setDrawerWidth((current) => clampDrawerWidth(current));
    const onWindowResize = () => setDrawerWidth((current) => clampDrawerWidth(current));
    window.addEventListener("resize", onWindowResize);
    return () => window.removeEventListener("resize", onWindowResize);
  }, [clampDrawerWidth, open]);

  useEffect(() => () => {
    window.removeEventListener("pointermove", onResizePointerMove);
    window.removeEventListener("pointerup", onResizePointerUp);
    stopResizing();
  }, [onResizePointerMove, onResizePointerUp, stopResizing]);

  const { data: trace, isLoading: isTraceLoading, error: traceError } = useTrace(traceId ?? "");
  const { data: spans, isLoading: isSpansLoading } = useTraceSpans(traceId ?? "", { enabled: open && !!traceId });
  const provider = resolveProviderFromMetadata(trace?.metadata);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className={`left-auto right-0 top-0 h-dvh max-h-dvh max-w-none translate-x-0 translate-y-0 rounded-none border-l border-elevation-subtle p-0 transition-[width,box-shadow] duration-150 data-[state=closed]:slide-out-to-right-full data-[state=open]:slide-in-from-right-full data-[state=closed]:zoom-out-100 data-[state=open]:zoom-in-100 ${isResizing ? "shadow-[-18px_0_36px_color-mix(in_srgb,_var(--color-accent)_calc(0.18_*_100%),_transparent)]" : ""}`}
        style={{ width: `${drawerWidth}px` }}
      >
        <button
          type="button"
          aria-label="Resize trace drawer"
          className="group absolute -left-1 top-0 z-20 h-full w-3 cursor-col-resize touch-none"
          onPointerDown={onResizePointerDown}
        >
          <span
            className={`absolute left-1/2 top-0 h-full -translate-x-1/2 rounded-full transition-all duration-150 ${isResizing ? "w-1.5 bg-accent shadow-[0_0_12px_color-mix(in_srgb,_var(--color-accent)_calc(0.8_*_100%),_transparent)]" : "w-0.5 bg-accent/45 group-hover:w-1 group-hover:bg-accent/80"}`}
          />
        </button>
        <DialogHeader className="border-b border-elevation-subtle-soft bg-elevation-hover px-3 py-2 text-left">
          <div className="flex items-start justify-between gap-3 pr-8">
            <div className="space-y-1">
              <DialogTitle className="text-lg">{trace?.name ?? "Trace details"}</DialogTitle>
              <DialogDescription className="text-[11px]">
                Trace ID: {traceId ?? "—"}
              </DialogDescription>
            </div>
            {trace ? (
              <div className="flex items-center gap-2">
                <Badge variant={trace.status === "ok" ? "secondary" : "destructive"}>{t_i18n(traceStatusLabelKey(trace.status))}</Badge>
                <Button asChild variant="outline" size="sm">
                  <Link to={`/observability/traces/${trace.id}`}>
                    {t_i18n("Open full page")}
                    <ExternalLink className="h-3.5 w-3.5" />
                  </Link>
                </Button>
              </div>
            ) : null}
          </div>
          {trace ? (
            <div className="mt-2 flex flex-wrap items-center gap-2 content-compact">
              <Badge variant="outline">Latency: {formatDuration(trace.duration_ms)}</Badge>
              {trace.conversation_id ? (
                <Badge variant="outline">Session: {trace.conversation_id.slice(0, 8)}…</Badge>
              ) : null}
              {trace.user_id ? (
                <Badge variant="outline">User ID: {trace.user_id.slice(0, 8)}…</Badge>
              ) : null}
              <Badge variant="outline">Env: {trace.environment || "default"}</Badge>
              <Badge variant="outline">Provider: {provider ?? "unknown"}</Badge>
              {/* Compact cost and tokens, the exact figures on hover (UX B3/B4 on #10529). */}
              <Badge variant="outline" title={costDataAvailable ? exactCompactCost(trace.total_cost) : undefined}>
                {costDataAvailable ? formatCompactCost(trace.total_cost) : <MaskedCost className="inline-flex" />}
              </Badge>
              <Badge
                variant="outline"
                title={exactMetricValue(trace.input_tokens) || exactMetricValue(trace.output_tokens)
                  ? `${exactMetricValue(trace.input_tokens) ?? trace.input_tokens} prompt → ${exactMetricValue(trace.output_tokens) ?? trace.output_tokens} completion`
                  : undefined}
              >
                {formatCompactNumber(trace.input_tokens)} prompt → {formatCompactNumber(trace.output_tokens)} completion
                {trace.total_tokens ? ` [Σ ${formatCompactNumber(trace.total_tokens)}]` : ""}
              </Badge>
              <Badge variant="outline">{trace.model || "default"}</Badge>
            </div>
          ) : null}
        </DialogHeader>

        <div className="h-[calc(100dvh-112px)] overflow-hidden bg-elevation-default px-3 py-2">
          {isTraceLoading ? (
            <PageSkeleton cards={2} />
          ) : !trace || traceError ? (
            <div className="rounded-sm border border-elevation-subtle-soft p-6 content-base text-default-secondary">
              <div className="mb-2 inline-flex items-center gap-2 font-medium text-default-primary">
                <Scan className="h-4 w-4" />
                Trace not found
              </div>
              This trace may have been deleted or you don't have access.
            </div>
          ) : (
            isSpansLoading ? <PageSkeleton cards={3} /> : <TraceExplorer spans={spans?.items ?? []} tags={trace.tags} trace={trace} />
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
