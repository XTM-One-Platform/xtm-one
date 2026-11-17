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
import { Scan } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { SpanNode, type SpanTreeNode } from "@/components/traces/span-node";
import type { LLMSpan } from "@/types";

function buildTree(spans: LLMSpan[]): SpanTreeNode[] {
  const byId = new Map<string, SpanTreeNode>(
    spans.map((span) => [
      span.id,
      {
        ...span,
        children: [],
      },
    ]),
  );
  const roots: SpanTreeNode[] = [];

  for (const span of spans) {
    const node = byId.get(span.id);
    if (!node) continue;
    if (span.parent_span_id && byId.has(span.parent_span_id)) {
      byId.get(span.parent_span_id)?.children.push(node);
    } else {
      roots.push(node);
    }
  }

  for (const node of byId.values()) {
    node.children.sort((a, b) => (a.position ?? 0) - (b.position ?? 0));
  }
  roots.sort((a, b) => (a.position ?? 0) - (b.position ?? 0));
  return roots;
}

interface TraceTreeProps {
  spans: LLMSpan[];
  selectedSpanId?: string | null;
  onSelectSpan?: (spanId: string) => void;
  bordered?: boolean;
  compact?: boolean;
  errorIterationIds?: ReadonlySet<string>;
}

export function TraceTree({
  spans,
  selectedSpanId,
  onSelectSpan,
  bordered = true,
  compact = false,
  errorIterationIds,
}: TraceTreeProps) {
  const roots = useMemo(() => buildTree(spans), [spans]);

  if (roots.length === 0) {
    return (
      <EmptyState
        icon={Scan}
        title="No spans found"
        description="This trace has no span tree yet."
      />
    );
  }

  return (
    <Card className={bordered ? undefined : "rounded-none border-0 bg-transparent shadow-none"}>
      <CardContent className="p-0">
        <div className="divide-y divide-[var(--border-elevation-subtle)]">
          {roots.map((node) => (
            <SpanNode
              key={node.id}
              node={node}
              depth={0}
              compact={compact}
              selectedSpanId={selectedSpanId}
              onSelectSpan={onSelectSpan}
              errorIterationIds={errorIterationIds}
            />
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
