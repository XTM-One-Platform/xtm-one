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
 * The node library panel.
 *
 * Left rail, collapsible, searchable, grouped by the catalog's own categories.
 * Every entry comes from `GET /flows/node-catalog` — there is no local list of
 * node types to keep in sync.
 *
 * Two ways to place a node, because both are muscle memory for anyone who has
 * used a canvas builder: drag onto the canvas, or click to drop one in the
 * middle of the current view.
 */

import { useMemo, useState } from "react";
import { Bookmark, Trash2, ChevronDown, ChevronRight, Lock, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { Button } from "@/components/ui/button";
import { SearchInput } from "@/components/ui/search-input";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import type { FlowNodeCatalog, FlowNodeKind } from "@/types";
import { NODE_DRAG_MIME } from "./FlowCanvas";
import { DEFAULT_NODE_ICON, FLOW_NODE_ICONS, nodeStyle } from "./node-visuals";
import type { FlowSnippet } from "./snippets";

interface NodeLibraryPanelProps {
  catalog: FlowNodeCatalog | undefined;
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
  /** Click-to-place: drops the node in the middle of the current viewport. */
  onPlace: (kind: string) => void;
  /** Saved selections (localStorage) — click to paste at the view centre. */
  snippets?: FlowSnippet[];
  onInsertSnippet?: (snippet: FlowSnippet) => void;
  onRemoveSnippet?: (id: string) => void;
  disabled?: boolean;
}

function matches(kind: FlowNodeKind, query: string): boolean {
  if (!query) return true;
  const haystack = [
    kind.label,
    kind.description,
    kind.kind,
    ...kind.searchable_terms,
  ]
    .join(" ")
    .toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((term) => haystack.includes(term));
}

function LibraryItem({
  kind,
  onPlace,
  disabled,
}: {
  kind: FlowNodeKind;
  onPlace: (kind: string) => void;
  disabled?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const aiLockedHint = t_i18n(
    "This step needs AI, which is disabled on this platform.",
  );
  const Icon = FLOW_NODE_ICONS[kind.icon ?? ""] ?? DEFAULT_NODE_ICON;
  const style = nodeStyle(kind.kind, kind.category);
  const aiOff = useAiActionDisabled();
  /* `?? true` mirrors the backend default: a node kind this build does not
     recognise must not be presented as safe to place (FR-030). */
  const locked = aiOff && (kind.requires_ai ?? true);

  return (
    <button
      type="button"
      draggable={!disabled && !locked}
      disabled={disabled || locked}
      title={
        locked
          ? `${kind.description}\n\n${aiLockedHint}`
          : kind.description
      }
      onDragStart={(event) => {
        event.dataTransfer.setData(NODE_DRAG_MIME, kind.kind);
        event.dataTransfer.effectAllowed = "move";
      }}
      onClick={() => onPlace(kind.kind)}
      className={cn(
        "flex w-full items-center gap-2.5 rounded-sm px-2 py-2 text-left transition-colors",
        "hover:bg-elevation-hover disabled:cursor-not-allowed disabled:opacity-50",
        !disabled && "cursor-grab active:cursor-grabbing",
      )}
    >
      <span
        className={cn(
          "flex h-7 w-7 shrink-0 items-center justify-center rounded-sm",
          style.chip,
        )}
      >
        <Icon className={cn("h-3.5 w-3.5", style.accent)} />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className="truncate content-compact font-medium">{kind.label}</span>
          {locked && (
            <Lock className="h-2.5 w-2.5 shrink-0 text-feedback-warning-primary" />
          )}
        </span>
        <span className="block truncate content-caption text-default-secondary">
          {kind.description}
        </span>
      </span>
    </button>
  );
}

export function NodeLibraryPanel({
  catalog,
  collapsed,
  onCollapsedChange,
  onPlace,
  snippets,
  onInsertSnippet,
  onRemoveSnippet,
  disabled,
}: NodeLibraryPanelProps) {
  const { t_i18n } = useFormatter();
  const [query, setQuery] = useState("");
  const [closed, setClosed] = useState<Set<string>>(new Set());

  const grouped = useMemo(() => {
    if (!catalog) return [];
    return catalog.categories
      .map((category) => ({
        ...category,
        kinds: catalog.node_kinds.filter(
          (k) =>
            k.category === category.id &&
            // The import fallback is not something anyone places by hand.
            k.kind !== "unresolved" &&
            matches(k, query),
        ),
      }))
      .filter((group) => group.kinds.length > 0);
  }, [catalog, query]);

  if (collapsed) {
    return (
      <div className="flex w-12 shrink-0 flex-col items-center border-r border-elevation-subtle layer-1 bg-elevation-default py-3">
        <Button
          variant="ghost"
          size="icon"
          onClick={() => onCollapsedChange(false)}
          aria-label={t_i18n("Show the node library")}
        >
          <PanelLeftOpen className="h-4 w-4" />
        </Button>
      </div>
    );
  }

  return (
    <div className="flex w-64 shrink-0 flex-col border-r border-elevation-subtle layer-1 bg-elevation-default">
      <div className="flex items-center gap-2 border-b border-elevation-subtle p-3">
        <SearchInput
          placeholder={t_i18n("Search nodes...")}
          value={query}
          onChange={setQuery}
          className="flex-1"
        />
        <Button
          variant="ghost"
          size="icon"
          onClick={() => onCollapsedChange(true)}
          aria-label={t_i18n("Hide the node library")}
        >
          <PanelLeftClose className="h-4 w-4" />
        </Button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {grouped.length === 0 ? (
          <p className="px-2 py-8 text-center content-compact text-default-secondary">
            {catalog
              ? t_i18n("No nodes matching your search")
              : t_i18n("Loading the node library…")}
          </p>
        ) : (
          grouped.map((group) => {
            // A search implicitly opens every group — hiding a match behind a
            // collapsed header makes the search look broken.
            const isOpen = !!query || !closed.has(group.id);
            return (
              <div key={group.id} className="mb-1">
                <button
                  type="button"
                  onClick={() =>
                    setClosed((prev) => {
                      const next = new Set(prev);
                      if (next.has(group.id)) next.delete(group.id);
                      else next.add(group.id);
                      return next;
                    })
                  }
                  className="flex w-full items-center gap-1.5 rounded-sm px-2 py-1.5 content-caption font-semibold uppercase tracking-wide text-default-secondary transition-colors hover:bg-elevation-hover"
                >
                  {isOpen ? (
                    <ChevronDown className="h-3 w-3" />
                  ) : (
                    <ChevronRight className="h-3 w-3" />
                  )}
                  {group.label}
                  <span className="ml-auto tabular-nums">{group.kinds.length}</span>
                </button>
                {isOpen &&
                  group.kinds.map((kind) => (
                    <LibraryItem
                      key={kind.kind}
                      kind={kind}
                      onPlace={onPlace}
                      disabled={disabled}
                    />
                  ))}
              </div>
            );
          })
        )}

        {/* Snippets: saved selections, pasted at the view centre. */}
        {(snippets?.length ?? 0) > 0 && (
          <div className="mb-1 mt-2 border-t border-elevation-subtle-soft pt-2">
            <p className="flex items-center gap-1.5 px-2 py-1.5 content-caption font-semibold uppercase tracking-wide text-default-secondary">
              <Bookmark className="h-3 w-3" /> {t_i18n("Snippets")}
              <span className="ml-auto tabular-nums">{snippets!.length}</span>
            </p>
            {snippets!.map((snippet) => (
              <div
                key={snippet.id}
                className="group/snip flex w-full items-center gap-1.5 rounded-sm px-2 py-1.5 hover:bg-elevation-hover"
              >
                <button
                  type="button"
                  disabled={disabled}
                  className="flex min-w-0 flex-1 items-center gap-2 text-left disabled:opacity-50"
                  title={t_i18n("{count} nodes", { count: snippet.nodes.length })}
                  onClick={() => onInsertSnippet?.(snippet)}
                >
                  <span className="truncate content-compact">{snippet.name}</span>
                  <span className="ml-auto shrink-0 content-caption tabular-nums text-default-secondary">
                    {snippet.nodes.length}
                  </span>
                </button>
                <button
                  type="button"
                  aria-label={t_i18n("Remove")}
                  className="opacity-0 transition-opacity group-hover/snip:opacity-100"
                  onClick={() => onRemoveSnippet?.(snippet.id)}
                >
                  <Trash2 className="h-3 w-3 text-default-secondary hover:text-feedback-error-primary" />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      <p className="border-t border-elevation-subtle px-3 py-2 content-caption text-default-secondary">
        {t_i18n("Drag onto the canvas, or click to place. Double-click the canvas to search.")}
      </p>
    </div>
  );
}
