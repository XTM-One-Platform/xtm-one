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

import { useMemo } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Coins, Scan } from "lucide-react";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { Badge } from "@/components/ui/badge";
import { MaskedCost } from "@/components/ui/masked-cost";
import { NotFoundState } from "@/components/ui/not-found-state";
import { PageSkeleton } from "@/components/ui/skeleton";
import { Tip } from "@/components/ui/tooltip";
import { TraceExplorer } from "@/components/traces/trace-explorer";
import { useCostDataAvailability } from "@/hooks/useCostDataAvailability";
import { useTrace, useTraceSessionExplorer, useTraceSpans } from "@/hooks/api/useTraceApi";
import {
  formatDuration,
  resolveProviderConfigFromMetadata,
  resolveProviderFromMetadata,
  traceStatusLabelKey,
} from "@/lib/trace-utils";
import { BACKEND_FETCH_LIMIT } from "@/lib/constants";
import { isTraceScopeId } from "@/lib/trace-links";
import RouteNotFound from "@/pages/RouteNotFound";
import { exactCompactCost, formatCompactCost, formatShortDateTime } from "@/lib/utils";
import { exactCount, formatCount } from "@/lib/number-format";
import { useFormatter } from "@/i18n";

export default function TraceDetail() {
  const { t_i18n, intl } = useFormatter();
  const costDataAvailable = useCostDataAvailability();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { id } = useParams<{ id: string }>();
  const requestedSessionId = searchParams.get("session_id");
  // Only a trace id is looked up: a segment such as `sessions` (the former
  // `/traces/sessions`) names a list endpoint of the API, not a trace (#822).
  const traceId = id && isTraceScopeId(id) ? id : "";
  const { data: trace, isLoading: isTraceLoading, error: traceError } = useTrace(traceId);
  const { data: spans, isLoading: isSpansLoading } = useTraceSpans(traceId, { enabled: !!traceId });
  const activeSessionId = requestedSessionId ?? trace?.conversation_id ?? null;
  const sessionExplorerQuery = useTraceSessionExplorer(
    activeSessionId ?? "",
    { enabled: !!activeSessionId, limit: BACKEND_FETCH_LIMIT },
  );
  const sessionTraces = useMemo(
    () => (activeSessionId ? (sessionExplorerQuery.data?.traces ?? []) : []),
    [activeSessionId, sessionExplorerQuery.data],
  );
  const sessionSpansByTraceId = useMemo(
    () =>
      !activeSessionId
        ? {}
        : (sessionExplorerQuery.data?.spans_by_trace_id ?? (trace?.id ? { [trace.id]: spans?.items ?? [] } : {})),
    [activeSessionId, sessionExplorerQuery.data, trace, spans],
  );
  const sessionSummary = useMemo(() => {
    if (!activeSessionId) return null;
    return {
      totalTraces: sessionExplorerQuery.data?.total_traces ?? sessionTraces.length,
      totalSpans: sessionExplorerQuery.data?.total_spans
        ?? sessionTraces.reduce((sum, item) => sum + (sessionSpansByTraceId[item.id]?.length ?? 0), 0),
    };
  }, [activeSessionId, sessionExplorerQuery.data, sessionSpansByTraceId, sessionTraces]);
  const provider = resolveProviderFromMetadata(trace?.metadata);
  const providerConfig = resolveProviderConfigFromMetadata(trace?.metadata);
  const quotaActionsConsumed = useMemo(() => {
    const metadata = trace?.metadata;
    if (!metadata || typeof metadata !== "object") return null;
    const consumed = (metadata as Record<string, unknown>).quota_actions_consumed;
    return typeof consumed === "number" ? consumed : null;
  }, [trace?.metadata]);
  const isSessionExplorerLoading = Boolean(
    activeSessionId
    && sessionExplorerQuery.isLoading,
  );

  const backTo = requestedSessionId ? "/observability/sessions" : "/observability/traces";
  const backLabel = requestedSessionId ? "Back to Sessions" : "Back to Traces";

  if (!traceId) return <RouteNotFound />;
  if (isTraceLoading) return <PageSkeleton cards={4} />;

  if (!trace || traceError) {
    return (
      <NotFoundState
        icon={Scan}
        title="Trace not found"
        description="This trace may have been deleted or you don't have access."
        actionLabel="View All Traces"
        onAction={() => navigate("/observability/traces")}
        backTo={backTo}
        backLabel={backLabel}
      />
    );
  }

  return (
    <div className="space-y-4">
      {/* No icon tile: a trace never had one, its facts follow in the box. */}
      <EntityPageHeader
        backTo={backTo}
        backLabel={backLabel}
        title={trace.name}
        description={t_i18n("Trace ID: {id}", { id: trace.id })}
        actions={<Badge variant={trace.status === "ok" ? "secondary" : "destructive"}>{t_i18n(traceStatusLabelKey(trace.status))}</Badge>}
      />

      <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2">
        <div className="flex flex-wrap items-center gap-2 content-compact">
          <Badge variant="outline">{t_i18n("Latency: {duration}", { duration: formatDuration(trace.duration_ms) })}</Badge>
          {trace.conversation_id ? (
            <Badge variant="outline">
              <Link to={`/chat?c=${trace.conversation_id}`} className="hover:underline">
                {t_i18n("Session: {id}...", { id: trace.conversation_id.slice(0, 8) })}
              </Link>
            </Badge>
          ) : null}
          {trace.assignment_run_id ? (
            <Badge variant="outline">
              <Link to={`/operations/executions/${trace.assignment_run_id}`} className="hover:underline">
                {t_i18n("Run: {id}...", { id: trace.assignment_run_id.slice(0, 8) })}
              </Link>
            </Badge>
          ) : null}
          {trace.agent_id ? (
            <Badge variant="outline">
              <Link to={`/agents/${trace.agent_id}`} className="hover:underline">
                {t_i18n("Agent: {name}", { name: trace.agent_name ?? trace.agent_id.slice(0, 8) })}
              </Link>
            </Badge>
          ) : null}
          <Badge variant="outline">{t_i18n("Env: {env}", { env: trace.environment || "default" })}</Badge>
          <Badge variant="outline">{t_i18n("Provider: {provider}", { provider: provider ?? t_i18n("unknown") })}</Badge>
          {providerConfig ? (
            <Tip content={providerConfig.id}>
              <Badge
                variant="outline"
                tabIndex={0}
                className="focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
              >
                {providerConfig.downgraded
                  ? t_i18n("Provider config: {config} (downgraded)", {
                      config: providerConfig.label ?? providerConfig.id.slice(0, 8),
                    })
                  : providerConfig.failover
                  ? t_i18n("Provider config: {config} (failover)", {
                      config: providerConfig.label ?? providerConfig.id.slice(0, 8),
                    })
                  : t_i18n("Provider config: {config}", {
                      config: providerConfig.label ?? providerConfig.id.slice(0, 8),
                    })}
              </Badge>
            </Tip>
          ) : null}
          {/* Compact cost and tokens, the exact figures on hover (UX B3/B4 on #10529). */}
          <Badge variant="outline" title={costDataAvailable ? exactCompactCost(trace.total_cost) : undefined}>
            {costDataAvailable ? formatCompactCost(trace.total_cost) : <MaskedCost className="inline-flex" />}
          </Badge>
          <Badge
            variant="outline"
            title={t_i18n("{input} prompt to {output} completion", {
              input: exactCount(intl.locale, trace.input_tokens),
              output: exactCount(intl.locale, trace.output_tokens),
            })}
          >
            {t_i18n("{input} prompt to {output} completion", {
              input: formatCount(intl.locale, trace.input_tokens),
              output: formatCount(intl.locale, trace.output_tokens),
            })}
            {trace.total_tokens ? ` ${t_i18n("[sum {total}]", { total: formatCount(intl.locale, trace.total_tokens) })}` : ""}
          </Badge>
          <Badge variant="outline">{trace.model || "default"}</Badge>
          {quotaActionsConsumed != null ? (
            <Badge
              variant="outline"
              className="border-feedback-alert-primary/40 text-feedback-warning-primary light:border-feedback-alert-primary/40 light:text-feedback-warning-primary"
            >
              <Coins className="mr-1 h-3 w-3" />
              {t_i18n("{count, plural, one {# platform action} other {# platform actions}}", { count: quotaActionsConsumed })}
            </Badge>
          ) : null}
          <Badge variant="outline">{t_i18n("Started: {date}", { date: formatShortDateTime(trace.started_at) })}</Badge>
          {trace.ended_at ? <Badge variant="outline">{t_i18n("Ended: {date}", { date: formatShortDateTime(trace.ended_at) })}</Badge> : null}
        </div>

        {activeSessionId ? (
          <div className="mt-3 border-t border-elevation-subtle-soft pt-2">
            <div className="mb-1 flex items-center gap-2 content-compact text-default-secondary">
              <span>{t_i18n("Session focus: {id}", { id: activeSessionId })}</span>
              {sessionExplorerQuery.isLoading ? <span>{t_i18n("Loading traces...")}</span> : null}
              {!sessionExplorerQuery.isLoading ? (
                <Badge variant="outline">{t_i18n("{count, plural, one {# trace} other {# traces}}", { count: sessionTraces.length })}</Badge>
              ) : null}
              {sessionExplorerQuery.data && sessionExplorerQuery.data.total_traces > sessionTraces.length ? (
                <Badge variant="outline">{t_i18n("+{count} more", { count: sessionExplorerQuery.data.total_traces - sessionTraces.length })}</Badge>
              ) : null}
            </div>
            {sessionTraces.length > 0 ? (
              <div className="flex flex-wrap gap-1.5">
                {sessionTraces.map((sessionTrace) => {
                  const isCurrent = sessionTrace.id === trace.id;
                  const label = sessionTrace.name?.trim() || sessionTrace.id.slice(0, 8);
                  return (
                    <Link
                      key={sessionTrace.id}
                      to={`/observability/traces/${sessionTrace.id}?session_id=${activeSessionId}`}
                      className={`rounded-sm border px-2 py-1 content-compact transition-colors ${
                        isCurrent
                          ? "border-elevation-default bg-elevation-highlight/15 text-default-primary"
                          : "border-elevation-subtle-soft text-default-secondary hover:bg-elevation-hover hover:text-default-primary"
                      }`}
                    >
                      {label}
                    </Link>
                  );
                })}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="h-[calc(100dvh-250px)] overflow-hidden">
        {isSpansLoading || isSessionExplorerLoading ? (
          <PageSkeleton cards={3} />
        ) : (
          <TraceExplorer
            spans={spans?.items ?? []}
            tags={trace.tags}
            trace={trace}
            traces={activeSessionId ? sessionTraces : undefined}
            spansByTraceId={activeSessionId ? sessionSpansByTraceId : undefined}
            sessionId={activeSessionId}
            sessionSummary={activeSessionId ? sessionSummary : undefined}
          />
        )}
      </div>
    </div>
  );
}
