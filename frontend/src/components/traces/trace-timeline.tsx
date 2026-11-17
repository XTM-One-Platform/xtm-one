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
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Scan } from "lucide-react";
import { formatDuration } from "@/lib/trace-utils";
import { formatShortDateTime } from "@/lib/utils";
import type { LLMSpan } from "@/types";

interface TraceTimelineProps {
  spans: LLMSpan[];
  selectedSpanId?: string | null;
  onSelectSpan?: (spanId: string) => void;
}

export function TraceTimeline({ spans, selectedSpanId, onSelectSpan }: TraceTimelineProps) {
  const ordered = useMemo(
    () => [...spans].sort((a, b) => Date.parse(a.started_at) - Date.parse(b.started_at)),
    [spans],
  );

  if (ordered.length === 0) {
    return (
      <EmptyState
        icon={Scan}
        title="No spans found"
        description="This trace has no timeline yet."
      />
    );
  }

  return (
    <Card>
      <CardContent className="p-0">
        <div className="divide-y divide-[var(--border-elevation-subtle)]">
          {ordered.map((span) => (
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
              <p className="mt-1 content-compact text-default-secondary">
                {formatShortDateTime(span.started_at)}{span.ended_at ? ` → ${formatShortDateTime(span.ended_at)}` : ""}
              </p>
            </button>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

