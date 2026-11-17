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

import { useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { SearchInput } from "@/components/ui/search-input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Info, Scan } from "lucide-react";
import { formatDuration } from "@/lib/trace-utils";
import { pluralize } from "@/lib/utils";
import type { LLMSpan } from "@/types";
import { useFormatter } from "@/i18n";

interface TraceSearchProps {
  spans: LLMSpan[];
  selectedSpanId?: string | null;
  onSelectSpan?: (spanId: string) => void;
}

type SpanLevelFilter = "all" | "ERROR" | "WARNING" | "INFO" | "DEBUG";
type SpanTypeFilter = "all" | "GENERATION" | "TOOL" | "ITERATION" | "ACTIVATION_GATE" | "MEMORY" | "SPAN";

function searchableText(span: LLMSpan): string {
  return [
    span.name,
    span.type,
    span.level,
    span.model ?? "",
    JSON.stringify(span.input ?? {}),
    JSON.stringify(span.output ?? {}),
    JSON.stringify(span.tool_arguments ?? {}),
  ].join(" ").toLowerCase();
}

/**
 * Payload fields `searchableText` actually reads, and can therefore under-match.
 *
 * `payload_meta` also describes `metadata`, which is not searched — counting it
 * would report a span as partly searchable when nothing searchable was held
 * back. (`tool_arguments` is searched but never bounded: the API sends it whole.)
 */
const SEARCHED_PAYLOAD_FIELDS = ["input", "output"] as const;

/**
 * Count the spans whose *searched* payloads reached this component bounded.
 *
 * `searchableText` can only match what the span response carried, and payloads
 * are previewed for transport — so a query for text that lives past the preview
 * boundary comes back "no matching spans", which reads as "not in this trace".
 * Saying how many spans are only partly searchable turns a silent false
 * negative into a visible limit, and points at the span inspector, where the
 * full payload can be loaded.
 */
function countBoundedSpans(spans: LLMSpan[]): number {
  return spans.filter((span) =>
    SEARCHED_PAYLOAD_FIELDS.some((field) => span.payload_meta?.[field]?.truncated),
  ).length;
}

export function TraceSearch({ spans, selectedSpanId, onSelectSpan }: TraceSearchProps) {
  const { t_i18n } = useFormatter();
  const [query, setQuery] = useState("");
  const [level, setLevel] = useState<SpanLevelFilter>("all");
  const [type, setType] = useState<SpanTypeFilter>("all");

  const boundedSpanCount = useMemo(() => countBoundedSpans(spans), [spans]);

  const filtered = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return spans.filter((span) => {
      if (level !== "all" && span.level !== level) return false;
      if (type !== "all" && span.type !== type) return false;
      if (!normalized) return true;
      return searchableText(span).includes(normalized);
    });
  }, [spans, query, level, type]);

  if (spans.length === 0) {
    return (
      <EmptyState icon={Scan} title="No spans found" description="This trace has no spans to search." />
    );
  }

  return (
    <div className="space-y-3">
      <div className="grid gap-2 sm:grid-cols-3">
        <SearchInput
          value={query}
          onChange={setQuery}
          placeholder={t_i18n("Search name, input, output, arguments...")}
        />
        <Select value={type} onValueChange={(value) => setType(value as SpanTypeFilter)}>
          <SelectTrigger>
            <SelectValue placeholder="Filter by type" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All types</SelectItem>
            <SelectItem value="ACTIVATION_GATE">ACTIVATION_GATE</SelectItem>
            <SelectItem value="GENERATION">GENERATION</SelectItem>
            <SelectItem value="ITERATION">ITERATION</SelectItem>
            <SelectItem value="MEMORY">MEMORY</SelectItem>
            <SelectItem value="SPAN">SPAN</SelectItem>
            <SelectItem value="TOOL">TOOL</SelectItem>
          </SelectContent>
        </Select>
        <Select value={level} onValueChange={(value) => setLevel(value as SpanLevelFilter)}>
          <SelectTrigger>
            <SelectValue placeholder="Filter by level" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All levels</SelectItem>
            <SelectItem value="ERROR">ERROR</SelectItem>
            <SelectItem value="WARNING">WARNING</SelectItem>
            <SelectItem value="INFO">INFO</SelectItem>
            <SelectItem value="DEBUG">DEBUG</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {boundedSpanCount > 0 ? (
        <div className="flex items-start gap-2 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2 content-compact text-default-secondary">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          {/* Phrased around the search rather than around the spans: a count
              that can be 1 drags verb and pronoun agreement along with it
              ("1 of 5 spans carry ... their payload"), and this says the same
              thing without any of it. */}
          <span>
            This search covers a bounded preview for {boundedSpanCount} of {spans.length}{" "}
            {pluralize(spans.length, "span")}, so text held back from a large payload will
            not match. Open a span and load its full payload to read the rest.
          </span>
        </div>
      ) : null}

      {filtered.length === 0 ? (
        <EmptyState
          icon={Scan}
          title="No matching spans"
          description={
            boundedSpanCount > 0
              ? "Try broadening your query or clearing filters. Text held back from a large payload will not match here."
              : "Try broadening your query or clearing filters."
          }
        />
      ) : (
        <Card>
          <CardContent className="p-0">
            <div className="divide-y divide-[var(--border-elevation-subtle)]">
              {filtered.map((span) => (
                <button
                  key={span.id}
                  type="button"
                  className={`w-full px-4 py-3 text-left transition-colors hover:bg-elevation-hover ${
                    selectedSpanId === span.id ? "bg-accent/10" : ""
                  }`}
                  onClick={() => onSelectSpan?.(span.id)}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="content-base font-medium">{span.name}</p>
                    <Badge variant="secondary">{span.type}</Badge>
                    <Badge variant={span.level === "ERROR" ? "destructive" : "outline"}>{span.level}</Badge>
                    <span className="ml-auto content-compact text-default-secondary">{formatDuration(span.duration_ms)}</span>
                  </div>
                </button>
              ))}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
