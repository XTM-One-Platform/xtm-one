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

import type { LLMSpan } from "@/types";

interface SpanTreeNode extends LLMSpan {
  children: SpanTreeNode[];
}

export function buildSpanTree(spans: LLMSpan[]): SpanTreeNode[] {
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

export function flattenSpanTree(roots: SpanTreeNode[]): SpanTreeNode[] {
  const ordered: SpanTreeNode[] = [];
  const stack = [...roots].reverse();
  while (stack.length > 0) {
    const node = stack.pop();
    if (!node) continue;
    ordered.push(node);
    for (let index = node.children.length - 1; index >= 0; index -= 1) {
      stack.push(node.children[index]);
    }
  }
  return ordered;
}

