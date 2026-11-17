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
 * Quick-add node search.
 *
 * Opened by double-clicking empty canvas or pressing Space. Drops the chosen
 * node exactly where the canvas was clicked, which is the difference between
 * this and clicking an item in the library panel (that drops in the middle of
 * the viewport).
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { FlowNodeCatalog, FlowNodeKind } from "@/types";
import { DEFAULT_NODE_ICON, FLOW_NODE_ICONS, nodeStyle } from "./node-visuals";

interface QuickAddMenuProps {
  catalog: FlowNodeCatalog | undefined;
  /** Screen coordinates the menu anchors to. */
  at: { x: number; y: number };
  /**
   * When set, only these node kinds are offered — used by drop-to-create so the
   * list is restricted to what can legally wire to the dragged handle.
   */
  allowedKinds?: Set<string>;
  onPick: (kind: string) => void;
  onClose: () => void;
}

export function QuickAddMenu({
  catalog,
  at,
  allowedKinds,
  onPick,
  onClose,
}: QuickAddMenuProps) {
  const { t_i18n } = useFormatter();
  const [query, setQuery] = useState("");
  const [highlighted, setHighlighted] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => inputRef.current?.focus(), []);

  const results = useMemo(() => {
    // Every placeable kind (scrollable) — never a truncated slice, or the list
    // silently hides node types the user is looking for.
    const kinds = (catalog?.node_kinds ?? []).filter(
      (k) =>
        k.kind !== "unresolved" && (!allowedKinds || allowedKinds.has(k.kind)),
    );
    if (!query) return kinds;
    const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
    return kinds.filter((k: FlowNodeKind) => {
      const haystack = [k.label, k.description, k.kind, ...k.searchable_terms]
        .join(" ")
        .toLowerCase();
      return terms.every((term) => haystack.includes(term));
    });
  }, [catalog, query, allowedKinds]);

  return (
    // Backdrop: a click anywhere else dismisses, and it stops the canvas from
    // panning underneath while the menu is open.
    <div
      className="fixed inset-0 z-30"
      onMouseDown={onClose}
      role="presentation"
    >
      <div
        data-testid="flow-quick-add"
        className="absolute w-72 overflow-hidden rounded-sm border border-elevation-subtle layer-2 bg-elevation-default text-default-primary shadow-overlay"
        style={{ left: at.x, top: at.y }}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <input
          ref={inputRef}
          value={query}
          placeholder={t_i18n("Add a node…")}
          onChange={(event) => {
            setQuery(event.target.value);
            setHighlighted(0);
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.stopPropagation();
              onClose();
            } else if (event.key === "ArrowDown") {
              event.preventDefault();
              setHighlighted((i) => (i + 1) % Math.max(results.length, 1));
            } else if (event.key === "ArrowUp") {
              event.preventDefault();
              setHighlighted(
                (i) => (i - 1 + results.length) % Math.max(results.length, 1),
              );
            } else if (event.key === "Enter" && results[highlighted]) {
              event.preventDefault();
              onPick(results[highlighted].kind);
            }
          }}
          className="w-full border-b border-elevation-subtle bg-transparent px-3 py-2 content-base placeholder:text-default-secondary focus:outline-none"
        />
        <div className="max-h-72 overflow-y-auto py-1">
          {results.length === 0 ? (
            <p className="px-3 py-3 text-[11px] text-default-secondary">
              {t_i18n("No nodes matching your search")}
            </p>
          ) : (
            results.map((kind, index) => {
              const Icon = FLOW_NODE_ICONS[kind.icon ?? ""] ?? DEFAULT_NODE_ICON;
              const style = nodeStyle(kind.kind, kind.category);
              return (
                <button
                  key={kind.kind}
                  type="button"
                  onMouseEnter={() => setHighlighted(index)}
                  onClick={() => onPick(kind.kind)}
                  className={cn(
                    "flex w-full items-center gap-2.5 px-3 py-1.5 text-left transition-colors",
                    index === highlighted && "bg-elevation-hover",
                  )}
                >
                  <Icon className={cn("h-3.5 w-3.5 shrink-0", style.accent)} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate content-compact">{kind.label}</span>
                    <span className="block truncate content-caption text-default-secondary">
                      {kind.description}
                    </span>
                  </span>
                </button>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
