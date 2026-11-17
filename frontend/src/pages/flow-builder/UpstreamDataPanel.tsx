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
 * "Input data": what the upstream nodes publish, as a tree, in the
 * node's drawer, so a reference is picked rather than guessed.
 *
 * Each row is an exact `{{...}}` reference the server accepts (it is built
 * from `variableSuggestions`, the autocomplete's own list), with its type and
 * the value the last run produced. Dragging a row drops its reference into a
 * text field (the browser inserts the dragged text); clicking copies it.
 */

import { useMemo, useState } from "react";
import { Check, ChevronDown, ChevronRight, GripVertical } from "lucide-react";
import { useFormatter } from "@/i18n";
import { ShapeTypeBadge } from "./ShapeTypeBadge";
import type { VariableSuggestion } from "./upstream";

interface NodeGroup {
  nodeId: string;
  nodeLabel: string;
  rows: (VariableSuggestion & { depth: number; name: string })[];
}

function groupByNode(suggestions: VariableSuggestion[]): NodeGroup[] {
  const groups = new Map<string, NodeGroup>();
  for (const suggestion of suggestions) {
    const [nodeId, ...path] = suggestion.ref.split(".");
    let group = groups.get(nodeId);
    if (!group) {
      group = { nodeId, nodeLabel: suggestion.nodeLabel, rows: [] };
      groups.set(nodeId, group);
    }
    // A list index (`hits.0.title`) has no row of its own: it is shown
    // inside its item's name (`0.title`) rather than as a missing level.
    const last = path.length - 1;
    const name = last > 0 && /^\d+$/.test(path[last - 1]) ? `${path[last - 1]}.${path[last]}` : path[last];
    const depth = path.filter((segment) => !/^\d+$/.test(segment)).length - 1;
    group.rows.push({ ...suggestion, depth, name });
  }
  return [...groups.values()];
}

export function UpstreamDataPanel({
  suggestions,
  directParentIds,
}: {
  suggestions: VariableSuggestion[];
  /** Nodes wired straight into this one: their group opens first. */
  directParentIds: Set<string>;
}) {
  const { t_i18n } = useFormatter();
  const groups = useMemo(() => groupByNode(suggestions), [suggestions]);
  const [open, setOpen] = useState(true);
  // Undefined = the default: direct parents open, the rest folded.
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [copied, setCopied] = useState<string | null>(null);

  const hasShapes = suggestions.some((s) => s.declared || s.observed);

  const copy = (ref: string) => {
    navigator.clipboard
      ?.writeText(`{{${ref}}}`)
      .then(() => {
        setCopied(ref);
        setTimeout(() => setCopied((current) => (current === ref ? null : current)), 1200);
      })
      // No clipboard access (permission, unfocused page): dragging still works.
      .catch(() => undefined);
  };

  return (
    <section className="rounded-sm border border-elevation-subtle">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 px-2.5 py-2 text-left content-compact font-medium"
      >
        {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        {t_i18n("Input data")}
        <span className="ml-auto content-caption font-normal text-default-secondary">
          {t_i18n("{count, plural, one {# node} other {# nodes}}", { count: groups.length })}
        </span>
      </button>
      {open && (
        <div className="space-y-1 border-t border-elevation-subtle px-1.5 py-1.5">
          {groups.length === 0 ? (
            <p className="px-1 py-1 content-caption text-default-secondary">
              {t_i18n("Connect this node to something upstream to reference its output.")}
            </p>
          ) : (
            <>
              <p className="px-1 content-caption text-default-secondary">
                {hasShapes
                  ? t_i18n("Drag a field into a text box, or click it to copy its reference.")
                  : t_i18n(
                      "Run the flow, or declare a structure (Structured output, trigger parameters, event type), to see the fields here.",
                    )}
              </p>
              {groups.map((group) => {
                const isOpen = expanded[group.nodeId] ?? directParentIds.has(group.nodeId);
                return (
                  <div key={group.nodeId}>
                    <button
                      type="button"
                      onClick={() =>
                        setExpanded((state) => ({ ...state, [group.nodeId]: !isOpen }))
                      }
                      className="flex w-full items-center gap-1 rounded-sm px-1 py-1 text-left text-[11px] hover:bg-elevation-hover"
                    >
                      {isOpen ? (
                        <ChevronDown className="h-3 w-3 shrink-0" />
                      ) : (
                        <ChevronRight className="h-3 w-3 shrink-0" />
                      )}
                      <span className="truncate">{group.nodeLabel}</span>
                      <span className="truncate font-mono-plex content-caption text-default-secondary">
                        {group.nodeId}
                      </span>
                    </button>
                    {isOpen &&
                      group.rows.map((row) => (
                        <button
                          key={row.ref}
                          type="button"
                          draggable
                          onDragStart={(event) => {
                            event.dataTransfer.setData("text/plain", `{{${row.ref}}}`);
                            event.dataTransfer.effectAllowed = "copy";
                          }}
                          onClick={() => copy(row.ref)}
                          title={row.description || `{{${row.ref}}}`}
                          style={{ paddingLeft: `${0.75 + row.depth * 0.75}rem` }}
                          className="group flex w-full cursor-grab items-center gap-1.5 rounded-sm py-0.5 pr-1 text-left hover:bg-elevation-hover active:cursor-grabbing"
                        >
                          <GripVertical className="h-3 w-3 shrink-0 text-default-secondary group-hover:text-default-secondary" />
                          <span className="shrink-0 font-mono-plex text-[11px] text-accent">{row.name}</span>
                          {row.sample !== undefined && row.sample !== null && (
                            <span className="min-w-0 truncate font-mono-plex content-caption text-default-secondary">
                              {String(row.sample)}
                            </span>
                          )}
                          {copied === row.ref ? (
                            <Check
                              aria-label={t_i18n("Copied")}
                              className="ml-auto h-3 w-3 shrink-0 text-accent"
                            />
                          ) : (
                            <ShapeTypeBadge
                              type={row.type}
                              declared={row.declared}
                              observed={row.observed}
                              className="ml-auto"
                            />
                          )}
                        </button>
                      ))}
                  </div>
                );
              })}
            </>
          )}
        </div>
      )}
    </section>
  );
}
