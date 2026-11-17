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

import { useEffect, useMemo, useState } from "react";
import { Clock3, GitBranch, Search, Share2 } from "lucide-react";
import { Input } from "@/components/ui/input";
import { TraceTree } from "@/components/traces/trace-tree";
import { TraceTimeline } from "@/components/traces/trace-timeline";
import { TraceSearch } from "@/components/traces/trace-search";
import { TraceGraph } from "@/components/traces/trace-graph";
import { SpanInspector } from "@/components/traces/span-inspector";
import type { LLMSpan, LLMTrace } from "@/types";

type TraceExplorerView = "tree" | "timeline" | "search" | "graph";

const VIEWS: { key: TraceExplorerView; label: string; icon: React.ElementType }[] = [
  { key: "tree", label: "Tree", icon: GitBranch },
  { key: "timeline", label: "Timeline", icon: Clock3 },
  { key: "search", label: "Search", icon: Search },
  { key: "graph", label: "Graph", icon: Share2 },
];

export function TraceExplorer({
  spans,
  tags,
  trace,
  traces,
  spansByTraceId,
  sessionId,
  sessionSummary,
}: {
  spans: LLMSpan[];
  tags?: string[];
  trace?: LLMTrace | null;
  traces?: LLMTrace[];
  spansByTraceId?: Record<string, LLMSpan[]>;
  sessionId?: string | null;
  sessionSummary?: { totalTraces: number; totalSpans: number } | null;
}) {
  const [view, setView] = useState<TraceExplorerView>("tree");
  const [selectedSpanId, setSelectedSpanId] = useState<string | null>(null);
  const [treeQuery, setTreeQuery] = useState("");

  const isSessionMode = Boolean(sessionId && traces?.length && spansByTraceId);

  const actualSpans = useMemo(() => {
    if (!isSessionMode || !traces || !spansByTraceId) return spans;
    return traces.flatMap((item) => spansByTraceId[item.id] ?? []);
  }, [isSessionMode, spans, spansByTraceId, traces]);

  const treeSpans = useMemo(() => {
    if (!isSessionMode || !traces || !spansByTraceId || !sessionId) return actualSpans;
    const sessionRootId = `session-root:${sessionId}`;
    const firstTrace = traces[0];
    if (!firstTrace) return actualSpans;

    const sessionRoot: LLMSpan = {
      id: sessionRootId,
      trace_id: firstTrace.id,
      parent_span_id: null,
      type: "SESSION",
      name: `Session ${sessionId}`,
      model: null,
      model_parameters: null,
      input: {},
      output: {},
      metadata: {
        synthetic: true,
        synthetic_kind: "session",
        session_id: sessionId,
      },
      level: "DEFAULT",
      status_message: null,
      started_at: firstTrace.started_at,
      ended_at: firstTrace.ended_at,
      duration_ms: firstTrace.duration_ms,
      time_to_first_token: null,
      input_tokens: null,
      output_tokens: null,
      input_cost: null,
      output_cost: null,
      total_cost: null,
      usage_details: null,
      tool_call_id: null,
      tool_arguments: null,
      stop_reason: null,
      position: -2,
      created_at: firstTrace.created_at,
    };

    const remappedActualSpans: LLMSpan[] = [];

    traces.forEach((item, index) => {
      const traceSpans = spansByTraceId[item.id] ?? [];
      const idsInTrace = new Set(traceSpans.map((span) => span.id));

      traceSpans.forEach((span) => {
        const hasKnownParent = span.parent_span_id && idsInTrace.has(span.parent_span_id);
        remappedActualSpans.push({
          ...span,
          parent_span_id: hasKnownParent ? span.parent_span_id : sessionRootId,
          // Keep a stable per-trace ordering for top-level roots under the
          // synthetic session node while preserving each span's local order.
          position: hasKnownParent
            ? span.position
            : index * 100_000 + (span.position ?? 0),
        });
      });
    });

    return [sessionRoot, ...remappedActualSpans];
  }, [actualSpans, isSessionMode, sessionId, spansByTraceId, traces]);

  useEffect(() => {
    if (actualSpans.length === 0) {
      setSelectedSpanId(null);
      return;
    }
    if (isSessionMode && sessionId) {
      const currentTracePreferred = trace?.id
        ? actualSpans.find((span) => span.trace_id === trace.id)?.id
        : null;
      setSelectedSpanId((current) => {
        if (current && treeSpans.some((span) => span.id === current)) return current;
        return currentTracePreferred ?? actualSpans[0]?.id ?? null;
      });
      return;
    }
    setSelectedSpanId((current) => {
      if (current && actualSpans.some((span) => span.id === current)) return current;
      return actualSpans[0]?.id ?? null;
    });
  }, [actualSpans, isSessionMode, sessionId, trace?.id, treeSpans]);

  const allSpansForSelection = useMemo(() => {
    if (!isSessionMode) return actualSpans;
    return treeSpans;
  }, [actualSpans, isSessionMode, treeSpans]);

  const selectedSpan = useMemo(
    () => allSpansForSelection.find((span) => span.id === selectedSpanId) ?? null,
    [allSpansForSelection, selectedSpanId],
  );

  const filteredTreeSpans = useMemo(() => {
    const normalized = treeQuery.trim().toLowerCase();
    if (!normalized) return treeSpans;
    return treeSpans.filter((span) => (
      `${span.name} ${span.type} ${span.level}`.toLowerCase().includes(normalized)
    ));
  }, [treeSpans, treeQuery]);

  const errorIterationIds = useMemo(() => {
    const ids = new Set<string>();
    const byId = new Map(actualSpans.map((span) => [span.id, span]));

    for (const span of actualSpans) {
      if (span.type !== "ITERATION") continue;
      if (span.level === "ERROR" || Boolean(span.status_message?.trim())) {
        ids.add(span.id);
      }
    }

    if (ids.size > 0) return ids;

    const hasErrorTrace = isSessionMode
      ? (traces ?? []).some((item) => item.status === "error")
      : trace?.status === "error";
    if (!hasErrorTrace) return ids;

    for (const span of actualSpans) {
      if (span.level !== "ERROR" && !span.status_message?.trim()) continue;
      let parentId = span.parent_span_id;
      while (parentId) {
        const parent = byId.get(parentId);
        if (!parent) break;
        if (parent.type === "ITERATION") {
          ids.add(parent.id);
          break;
        }
        parentId = parent.parent_span_id;
      }
    }

    if (ids.size === 0) {
      const sortedIterations = [...actualSpans]
        .filter((span) => span.type === "ITERATION")
        .sort((a, b) => (a.position ?? 0) - (b.position ?? 0));
      const lastIteration = sortedIterations[sortedIterations.length - 1];
      if (lastIteration) ids.add(lastIteration.id);
    }

    return ids;
  }, [actualSpans, isSessionMode, trace?.status, traces]);

  const renderView = () => {
    if (view === "timeline") {
      return <TraceTimeline spans={actualSpans} selectedSpanId={selectedSpanId} onSelectSpan={setSelectedSpanId} />;
    }
    if (view === "search") {
      return <TraceSearch spans={actualSpans} selectedSpanId={selectedSpanId} onSelectSpan={setSelectedSpanId} />;
    }
    if (view === "graph") {
      return <TraceGraph spans={actualSpans} selectedSpanId={selectedSpanId} onSelectSpan={setSelectedSpanId} />;
    }
    return (
      <TraceTree
        spans={filteredTreeSpans}
        selectedSpanId={selectedSpanId}
        onSelectSpan={setSelectedSpanId}
        bordered={false}
        compact
        errorIterationIds={errorIterationIds}
      />
    );
  };

  return (
    <div className="grid h-full gap-0 overflow-hidden rounded-sm border border-elevation-subtle-soft bg-elevation-default lg:grid-cols-[360px_minmax(0,1fr)]">
      <div className="flex h-full min-h-0 flex-col border-r border-elevation-subtle-soft bg-elevation-hover">
        <div className="flex items-center gap-2 border-b border-elevation-subtle-soft px-3 py-2">
          <Search className="h-4 w-4 text-default-secondary" />
          <Input aria-label="Search"
            value={treeQuery}
            onChange={(event) => setTreeQuery(event.target.value)}
            placeholder="Search"
            className="h-8 border-none bg-transparent p-0 content-base focus-visible:ring-0"
          />
          <span className="ml-auto content-compact text-default-secondary">Timeline</span>
        </div>
        <div className="border-b border-elevation-subtle-soft px-2 py-1">
          <div className="inline-flex rounded-sm bg-elevation-hover p-0.5">
          {VIEWS.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              type="button"
              className={`inline-flex items-center gap-1 rounded-sm px-2 py-1 content-compact transition-colors ${
                view === key
                  ? "bg-elevation-highlight text-default-primary"
                  : "text-default-secondary hover:bg-elevation-hover hover:text-default-primary"
              }`}
              onClick={() => setView(key)}
            >
              <Icon className="h-3.5 w-3.5" />
              {label}
            </button>
          ))}
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {renderView()}
        </div>
      </div>
      <div className="min-h-0 overflow-y-auto bg-elevation-default">
        <SpanInspector
          span={selectedSpan}
          spans={actualSpans}
          tags={tags}
          trace={trace}
          traces={isSessionMode ? traces : (trace ? [trace] : [])}
          sessionSummary={isSessionMode ? sessionSummary : null}
        />
      </div>
    </div>
  );
}
