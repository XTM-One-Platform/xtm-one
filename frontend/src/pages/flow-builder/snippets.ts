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
 * Snippet library — save a selection, paste it anywhere.
 *
 * Snippets live in localStorage (per browser, per user profile): they are a
 * personal clipboard that survives sessions, not a shared entity — the
 * platform-level sharing story is flows themselves (export / Collapse).
 * Positions are normalised to the origin on save so an insert can drop the
 * shape wherever the viewport is.
 */

import type { FlowEdge, FlowNode } from "@/types";

export interface FlowSnippet {
  id: string;
  name: string;
  nodes: FlowNode[];
  edges: FlowEdge[];
  createdAt: string;
}

const STORAGE_KEY = "xtm-flow-builder-snippets";
const MAX_SNIPPETS = 50;

export function loadSnippets(): FlowSnippet[] {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? (parsed as FlowSnippet[]) : [];
  } catch {
    return [];
  }
}

function persist(snippets: FlowSnippet[]): void {
  try {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify(snippets.slice(0, MAX_SNIPPETS)),
    );
  } catch {
    // Quota exceeded: the snippet simply is not saved; the caller toasts.
  }
}

export function saveSnippet(
  name: string,
  nodes: FlowNode[],
  edges: FlowEdge[],
): FlowSnippet {
  // Normalise to the origin so inserts can position freely.
  const minX = Math.min(...nodes.map((n) => n.position.x));
  const minY = Math.min(...nodes.map((n) => n.position.y));
  const snippet: FlowSnippet = {
    id: `snip_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`,
    name: name.trim() || "Snippet",
    nodes: nodes.map((n) => ({
      ...n,
      position: { x: n.position.x - minX, y: n.position.y - minY },
    })),
    edges,
    createdAt: new Date().toISOString(),
  };
  persist([snippet, ...loadSnippets()]);
  return snippet;
}

export function removeSnippet(id: string): FlowSnippet[] {
  const next = loadSnippets().filter((s) => s.id !== id);
  persist(next);
  return next;
}
