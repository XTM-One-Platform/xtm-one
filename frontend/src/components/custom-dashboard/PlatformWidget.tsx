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
 * A widget of OpenCTI, OpenAEV or OpenCRQ inside a dashboard (`kind: "platform"`),
 * or one drawing a data source the dashboard holds itself (`kind: "data"`).
 *
 * The data is the platform's, read by the server when the widget is drawn,
 * with the viewer's own access to it - never data written into the dashboard.
 * Each answer that is not data is one sentence and, when there is somewhere to
 * go, a link to the platform. Outside a stored dashboard (no
 * `DashboardLiveContext`) or with the Dashboard Studio off, the widget says
 * what it is and draws nothing.
 */
import { lazy, Suspense, useEffect, useMemo } from "react";
import { Spinner } from "@filigran/design-system";
import { ExternalLink, MessageCircleQuestion, Radar, RefreshCw } from "lucide-react";

import { useWidgetSize } from "@/components/dashboard-grid/widget-size-context";
import { AiActionGate } from "@/components/ui/ai-disabled";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useDashboardAccess } from "@/hooks/api/dashboard-access";
import { useAskDataSource, useDataSourceData, useRefreshSnapshot, useWidgetData } from "@/hooks/api/useCustomDashboardApi";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { useFormatter } from "@/i18n";
import { formatCount } from "@/lib/number-format";
import { safeExternalUrl, timeAgo } from "@/lib/utils";
import type { WidgetChanges, WidgetDataErrorCode, WidgetSnapshot } from "@/types";

import { accentStyle } from "./charts/colors";
import { presentedOptions } from "./definition";
import { withoutLinks } from "./viz/shapes";
import { chartHeight } from "./grid";
import { useDashboardTokens } from "./token-state";
import { useDashboardLive } from "./live";
import { hasNoData, type ClickEvent } from "./interactions";
import { tokensFor } from "./tokens";
import { ERROR_MESSAGES } from "./live-errors";
import { PLATFORM_NAMES, type DashboardWidget } from "./spec";
import { drawnAsChart } from "./viz/route";

const PlatformChart = lazy(() => import("./charts/PlatformChart"));
const StudioViz = lazy(() => import("./charts/StudioViz"));

type PlatformDashboardWidget = Extract<DashboardWidget, { kind: "platform" }>;
type DataDashboardWidget = Extract<DashboardWidget, { kind: "data" }>;


/** A refusal to ask (`dashboard_sources.AskRefusedError`). @i18n-keys */
const ASK_REFUSALS = {
  access_denied: "You cannot chat with this agent.",
  rate_limited: "Too many questions at once; try again in a minute.",
  upstream_error: "The question could not be asked.",
} as const satisfies Partial<Record<WidgetDataErrorCode, string>>;

/** Ask an `ask` source's question as oneself, and say why when it cannot be asked. */
function AskButton({ sourceId, again }: { sourceId: string; again?: boolean }) {
  const { t_i18n } = useFormatter();
  const live = useDashboardLive();
  const ask = useAskDataSource(live?.dashboardId);
  const access = useDashboardAccess();
  const code = ask.data?.status === "error" ? ask.data.code : undefined;
  // An export or a published snapshot asks nothing: only a person in the app does.
  if (access.kind !== "session") return null;
  const refused = code && code !== "running" ? ((ASK_REFUSALS as Partial<Record<WidgetDataErrorCode, string>>)[code] ?? ASK_REFUSALS.upstream_error) : undefined;
  return (
    <div className="flex flex-wrap items-center gap-2">
      {/* Asking runs the agent: unavailable, and saying why, while AI is off. */}
      <AiActionGate>
        <Button
          size="sm"
          variant={again ? "ghost" : "default"}
          className="gap-1.5"
          disabled={ask.isPending}
          aria-busy={ask.isPending || undefined}
          onClick={() => ask.mutate(sourceId)}
        >
          {ask.isPending && <Spinner size="sm" tone="inherit" />}
          {!ask.isPending && <MessageCircleQuestion className="h-3.5 w-3.5" aria-hidden="true" />}
          {again ? t_i18n("Ask again") : t_i18n("Ask the agent")}
        </Button>
      </AiActionGate>
      {(refused || ask.isError) && (
        <span role="alert" className="content-caption text-feedback-error-primary">
          {t_i18n(refused ?? ASK_REFUSALS.upstream_error)}
        </span>
      )}
    </div>
  );
}

function Placeholder({ widget }: { widget: PlatformDashboardWidget }) {
  const { t_i18n } = useFormatter();
  const platform = PLATFORM_NAMES[widget.source];
  return (
    <Card className="flex h-full min-h-28 flex-col justify-center gap-1.5 border-dashed p-5">
      <span className="flex items-center gap-2 content-caption uppercase tracking-wider text-default-secondary">
        <Radar className="h-3.5 w-3.5 text-accent" aria-hidden="true" />
        {platform} · {widget.nativeType}
      </span>
      <span className="content-compact text-default-secondary">
        {t_i18n("Live data from {platform}, read with your own access.", { platform })}
      </span>
    </Card>
  );
}

function OpenLink({ href, platform }: { href: string | undefined; platform: string }) {
  const { t_i18n } = useFormatter();
  const live = useDashboardLive();
  if (!href || live?.hideOpenLinks) return null;
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap content-caption text-accent hover:underline"
    >
      {t_i18n("Open in {platform}", { platform })}
      <ExternalLink className="h-3 w-3" aria-hidden="true" />
    </a>
  );
}

function LiveWidget({ widget, dashboardId }: { widget: PlatformDashboardWidget | DataDashboardWidget; dashboardId: string }) {
  const { t_i18n, locale } = useFormatter();
  const live = useDashboardLive();
  // A dashboard's own data names no platform: its rare errors are XTM One's.
  const platform = widget.kind === "platform" ? PLATFORM_NAMES[widget.source] : "XTM One";
  const page = useDashboardTokens();
  const used = useMemo(() => tokensFor(widget.chain, page?.tokens ?? {}), [widget.chain, page?.tokens]);
  const held = page?.held ?? false;
  const query = useWidgetData(dashboardId, widget.id, live?.range, widget.fingerprint, !held, widget.refresh, used);
  // What the visualization's handlers react to: a click on what it draws, a brush over a series.
  const handlers = useMemo(() => widget.handlers ?? [], [widget.handlers]);
  const runHandlers = page?.runHandlers;
  const clicks = handlers.some((handler) => handler.type !== "drilldown.setTimeRange");
  const brushes = handlers.some((handler) => handler.type === "drilldown.setTimeRange");
  const openUrl = query.data?.open_url;
  const onSelect = useMemo(
    () => (clicks && runHandlers ? (event: ClickEvent) => runHandlers(handlers, event, openUrl) : undefined),
    [clicks, runHandlers, handlers, openUrl],
  );
  const onBrush = useMemo(
    () => (brushes && runHandlers ? (range: { start: string; end: string }) => runHandlers(handlers, { range }, openUrl) : undefined),
    [brushes, runHandlers, handlers, openUrl],
  );
  const noData = hasNoData(query.data);
  const reportEmpty = page?.reportEmpty;
  useEffect(() => {
    if (noData !== undefined) reportEmpty?.(widget.id, noData);
  }, [noData, reportEmpty, widget.id]);
  const asks = widget.kind === "data" && widget.sourceType === "ask";
  const annotation = useDataSourceData(dashboardId, widget.annotation?.id, live?.range, widget.annotation?.fingerprint ?? "");
  // The chart fills the widget's cell on the grid, less its description; stacked, it takes its old height.
  const height = Math.max(80, chartHeight(widget.kind === "platform" ? widget.size : "m", useWidgetSize()) - (widget.description ? 24 : 0));

  if (!widget.view) {
    return (
      <Card className="flex h-full min-h-28 flex-col justify-center gap-2 border-dashed p-5">
        <span className="content-compact text-default-secondary">
          {t_i18n(ERROR_MESSAGES.unsupported, { platform })}
        </span>
        <OpenLink href={safeExternalUrl(query.data?.open_url)} platform={platform} />
      </Card>
    );
  }
  if (held) {
    return (
      <Card className="flex h-full min-h-28 flex-col justify-center gap-2 border-dashed p-5" data-error-code="waiting">
        <span className="content-compact text-default-secondary">{t_i18n(ERROR_MESSAGES.waiting, { platform })}</span>
      </Card>
    );
  }
  if (query.isPending) {
    return (
      <Card className="p-4" aria-busy="true" aria-label={t_i18n("Loading live data")}>
        <div style={{ height }}>
          <Skeleton className="h-full w-full" />
        </div>
      </Card>
    );
  }
  const data = query.data;
  if (query.isError || !data || data.status === "error") {
    const code: WidgetDataErrorCode = data?.status === "error" ? data.code : "upstream_error";
    return (
      <Card className="flex h-full min-h-28 flex-col justify-center gap-2 border-dashed p-5" data-error-code={code}>
        <span className="flex items-center gap-2 content-compact text-default-secondary">
          {code === "running" && <Spinner size="sm" tone="inherit" />}
          {/* A source refreshed on the server that was not read yet waits for the platform, not for an agent. */}
          {code === "running" && data?.snapshot ? t_i18n("Read on the server in a moment.") : t_i18n(ERROR_MESSAGES[code], { platform })}
        </span>
        {asks && widget.kind === "data" && code !== "running" && <AskButton sourceId={widget.sourceId} />}
        <OpenLink href={safeExternalUrl(data?.open_url)} platform={platform} />
      </Card>
    );
  }
  const annotationData = annotation.data?.status === "ok" ? annotation.data : undefined;
  // The link into the platform is in the widget's menu, as the modules keep theirs in the card's header.
  const footer = widget.showLastUpdated && data.fetched_at;
  const options = presentedOptions(widget.options, widget.view, data.presentation);
  // A dashboard that hides the links into the platforms hides its items' too.
  const shown = live?.hideOpenLinks ? withoutLinks(data) : data;
  return (
    <Card className="relative flex h-full flex-col gap-3 overflow-hidden p-4">
      {widget.showProgressBar && query.isFetching && (
        <span
          role="progressbar"
          aria-label={t_i18n("Refreshing the data")}
          className="absolute inset-x-0 top-0 h-0.5 animate-pulse bg-accent"
        />
      )}
      {widget.description && <p className="content-compact text-default-secondary">{widget.description}</p>}
      {/* The content in its module's accent, when its look names one; the frame keeps the platform's. */}
      <div className="contents" style={accentStyle(options)} data-accent={typeof options?.accentColor === "string" ? options.accentColor : undefined}>
        <Suspense
          fallback={
            <div style={{ height }}>
              <Skeleton className="h-full w-full" />
            </div>
          }
        >
          {drawnAsChart(shown, widget.view, options) ? (
            <PlatformChart
              data={shown}
              view={widget.view}
              height={height}
              options={options}
              context={widget.context}
              annotation={annotationData}
              onSelect={onSelect}
              onBrush={onBrush}
            />
          ) : (
            <StudioViz data={shown} view={widget.view === "text" ? undefined : widget.view} height={height} options={options} />
          )}
        </Suspense>
      </div>
      {asks && widget.kind === "data" && <AskButton sourceId={widget.sourceId} again />}
      {data.snapshot ? (
        <SnapshotLine widget={widget} snapshot={data.snapshot} changes={data.changes ?? null} />
      ) : (
        footer && (
          <span className="mt-auto min-w-0 truncate content-caption text-default-secondary">
            {widget.kind === "platform" ? `${platform} · ` : null}
            {t_i18n("Updated {when}", { when: timeAgo(data.fetched_at, locale) })}
          </span>
        )
      )}
    </Card>
  );
}

export function PlatformWidget({ widget }: { widget: PlatformDashboardWidget }) {
  const live = useDashboardLive();
  const studio = useFeatureFlag("DASHBOARD_STUDIO");
  if (!live || !studio) return <Placeholder widget={widget} />;
  return <LiveWidget widget={widget} dashboardId={live.dashboardId} />;
}

/** A visualization of a data source the dashboard holds (static data): read like a platform widget. */
export function DataWidget({ widget }: { widget: DataDashboardWidget }) {
  const { t_i18n } = useFormatter();
  const live = useDashboardLive();
  const studio = useFeatureFlag("DASHBOARD_STUDIO");
  if (!live || !studio) {
    return (
      <Card className="flex h-full min-h-28 flex-col justify-center gap-1.5 border-dashed p-5">
        <span className="content-compact text-default-secondary">{t_i18n("Data of this dashboard.")}</span>
      </Card>
    );
  }
  return <LiveWidget widget={widget} dashboardId={live.dashboardId} />;
}

/**
 * A widget drawing a source refreshed on the server says whose access read it
 * and when (decision D1), and offers Refresh now on the page of a signed-in
 * viewer; a published link's snapshot says it on its own page.
 */
function SnapshotLine({ widget, snapshot, changes }: { widget: DashboardWidget; snapshot: WidgetSnapshot; changes: WidgetChanges | null }) {
  const { t_i18n, locale } = useFormatter();
  const { dashboardId } = useDashboardLive() ?? {};
  const access = useDashboardAccess();
  const refresh = useRefreshSnapshot(dashboardId);
  const sourceId = widget.kind === "data" || widget.kind === "platform" ? widget.sourceId : undefined;
  if (!snapshot.refreshed_at) return null;
  return (
    <span className="mt-auto flex min-w-0 flex-col gap-0.5 content-caption text-default-secondary" data-testid="widget.snapshot">
      {changes && <ChangesLine changes={changes} />}
      <span className="flex min-w-0 items-center gap-1">
      <span className="min-w-0 truncate">
        {t_i18n("Data read {time} with {name}'s access.", { time: timeAgo(snapshot.refreshed_at, locale), name: snapshot.publisher ?? "?" })}
      </span>
      {access.kind === "session" && sourceId && (
        <Button
          variant="ghost"
          size="sm"
          className="h-6 w-6 shrink-0 px-0"
          aria-label={t_i18n("Refresh now")}
          disabled={refresh.isPending}
          onClick={() => refresh.mutate(sourceId)}
        >
          <RefreshCw className={refresh.isPending ? "h-3 w-3 animate-spin" : "h-3 w-3"} />
        </Button>
      )}
      </span>
    </span>
  );
}

/**
 * What changed since the previous read, said from the two reads alone
 * (`dashboard_refresh.with_changes`): the figure before and after, and for a
 * breakdown the items that moved the most.
 */
function ChangesLine({ changes }: { changes: WidgetChanges }) {
  const { t_i18n, locale } = useFormatter();
  const number = (value: number) => formatCount(locale, value);
  const { before, after } = changes.figure;
  const delta = after - before;
  const moved = (changes.items ?? []).slice(0, 3).map((item) => `${item.label} ${number(item.before)} → ${number(item.after)}`);
  return (
    <span className="min-w-0 truncate" data-testid="widget.changes">
      {delta === 0
        ? t_i18n("Unchanged since {time}.", { time: timeAgo(changes.since, locale) })
        : t_i18n("{before} → {after} since {time}.", { before: number(before), after: number(after), time: timeAgo(changes.since, locale) })}
      {moved.length > 0 && ` ${moved.join(" · ")}`}
    </span>
  );
}
