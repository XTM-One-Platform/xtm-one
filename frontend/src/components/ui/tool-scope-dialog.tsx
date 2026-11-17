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

import { useState } from "react";
import { Spinner } from "@filigran/design-system";
import { Check, Save, Wrench } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from "@/components/ui/dialog";
import { SearchInput } from "@/components/ui/search-input";
import { useFormatter } from "@/i18n";

// ---------------------------------------------------------------------------
// ToolScopeDialog: shared tool selection dialog used by AgentDetail and
// AssignmentDetail for scoping which tools from an integration or MCP server
// (or which functions of a built-in group) are available to an agent /
// assignment.
// ---------------------------------------------------------------------------

/**
 * The colour of the dialog's note (the parent agent's scoping). Warning primary
 * misses 4.5:1 on the light dialog surfaces (4.40:1 on #F4F4F6), so the light
 * theme takes the tertiary, as on a hovered row. Read by
 * tests_unit/warning-text-contrast.test.ts.
 */
export const TOOL_SCOPE_NOTE_TEXT = "text-feedback-warning-primary light:text-feedback-warning-tertiary";

/** What the dialog lists: an integration's tools, or a built-in group's functions. */
export type ToolScopeUnit = "tool" | "function";

interface ToolScopeDialogProps {
  /** Resolved list of available tools (caller handles loading). A `label`
   *  is the row's primary text, the `name` (the function id) then secondary;
   * without one the name is the primary text. */
  tools: { name: string; description: string; label?: string }[];
  /** Whether tools are still loading. */
  isLoading: boolean;
  /** Currently allowed tool names, or null for "all tools". */
  currentAllowed: string[] | null;
  /** Display name of the source (integration or MCP server name). */
  sourceLabel: string;
  /** Type label shown in description (e.g. "integration", "MCP server"). */
  sourceType: string;
  /** Context label for warnings (e.g. "agent", "assignment"). */
  contextLabel: string;
  /** Unit noun of the listed entries (default "tool"; a built-in group lists "function"s). */
  unit?: ToolScopeUnit;
  /** Optional note rendered below the description (e.g. agent-level scoping), in the warning note colour. */
  note?: string;
  /** Called with the selected tool names, or null for "all tools". */
  onSave: (allowed: string[] | null) => void;
  /** Called when the dialog is dismissed without saving. */
  onClose: () => void;
}

export function ToolScopeDialog({
  tools,
  isLoading,
  currentAllowed,
  sourceLabel,
  sourceType,
  contextLabel,
  unit = "tool",
  note,
  onSave,
  onClose,
}: ToolScopeDialogProps) {
  const fn = unit === "function";
  const { t_i18n } = useFormatter();
  const [selected, setSelected] = useState<Set<string>>(new Set(currentAllowed ?? []));
  const [allTools, setAllTools] = useState(currentAllowed === null);
  const [search, setSearch] = useState("");

  // Normalize: if a previously-saved explicit list covers all available tools, treat as "All tools"
  // Uses adjust-state-during-render pattern — only triggers when tool count changes (e.g. after async load).
  const toolCount = tools.length;
  const [prevToolCount, setPrevToolCount] = useState(toolCount);
  if (prevToolCount !== toolCount) {
    setPrevToolCount(toolCount);
    if (!allTools && toolCount > 0 && selected.size >= toolCount && tools.every((t) => selected.has(t.name))) {
      setAllTools(true);
      setSelected(new Set());
    }
  }

  const q = search.trim().toLowerCase();
  const filteredTools = q
    ? tools.filter(
        (t) =>
          t.name.toLowerCase().includes(q) ||
          t.label?.toLowerCase().includes(q) ||
          t.description?.toLowerCase().includes(q),
      )
    : tools;

  const toggleTool = (name: string) => {
    if (allTools) {
      // Transition from "all tools" to manual mode with everything except the clicked tool
      const allExcept = new Set(tools.map((t) => t.name));
      allExcept.delete(name);
      setSelected(allExcept);
      setAllTools(false);
      return;
    }
    const next = new Set(selected);
    if (next.has(name)) next.delete(name); else next.add(name);
    // Auto-switch to "All tools" when every tool is now selected
    if (tools.length > 0 && next.size >= tools.length && tools.every((t) => next.has(t.name))) {
      setAllTools(true);
      setSelected(new Set());
    } else {
      setSelected(next);
    }
  };

  const handleSelectAll = () => {
    setAllTools(true);
    setSelected(new Set());
  };

  const handleDeselectAll = () => {
    setAllTools(false);
    setSelected(new Set());
  };

  const handleSelectMatching = () => {
    const next = new Set(selected);
    filteredTools.forEach((t) => next.add(t.name));
    if (tools.length > 0 && next.size >= tools.length && tools.every((t) => next.has(t.name))) {
      setAllTools(true);
      setSelected(new Set());
    } else {
      setAllTools(false);
      setSelected(next);
    }
  };

  const handleDeselectMatching = () => {
    if (allTools && selected.size === 0) {
      // When allTools is true and selected is empty, it means "all tools selected".
      // To deselect only the matching tools, start from the full set of tool names
      // and remove the filtered ones, then switch to manual mode with that set.
      const next = new Set(tools.map((t) => t.name));
      filteredTools.forEach((t) => next.delete(t.name));
      setAllTools(false);
      setSelected(next);
      return;
    }

    const next = new Set(selected);
    filteredTools.forEach((t) => next.delete(t.name));
    setAllTools(false);
    setSelected(next);
  };

  const handleSave = () => {
    // Normalize: all-selected (regardless of method) always saves as null
    if (allTools || (tools.length > 0 && selected.size >= tools.length && tools.every((t) => selected.has(t.name)))) {
      onSave(null);
    } else {
      onSave(Array.from(selected));
    }
  };

  const selectedInFiltered = filteredTools.filter((t) => allTools || selected.has(t.name)).length;

  return (
    <Dialog open onOpenChange={() => onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Wrench className="h-5 w-5 text-accent" /> {t_i18n(fn ? "Function Scope: {name}" : "Tool Scope: {name}", { name: sourceLabel })}
          </DialogTitle>
          <DialogDescription>
            {t_i18n(fn
              ? "Choose which functions from this {sourceType} are available to the {contextLabel}. Uncheck functions to restrict the {contextLabel}'s access."
              : "Choose which tools from this {sourceType} are available to the {contextLabel}. Uncheck tools to restrict the {contextLabel}'s access.", { sourceType: t_i18n(sourceType), contextLabel: t_i18n(contextLabel) })}
            {note && (
              <span data-testid="tool-scope-note" className={cn("block mt-1", TOOL_SCOPE_NOTE_TEXT)}>{note}</span>
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 mt-2">
          {/* Search */}
          {!isLoading && tools.length > 0 && (
            <SearchInput
              placeholder={t_i18n(fn ? "Search functions…" : "Search tools…")}
              value={search}
              onChange={setSearch}
              className="max-w-full"
            />
          )}

          {/* Select all / deselect all */}
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 flex-wrap">
              <button
                className={cn(
                  "rounded-sm px-2.5 py-1 text-[11px] font-medium transition-colors",
                  allTools
                    ? "bg-accent/10 text-accent border border-accent/30"
                    : "bg-elevation-hover text-default-secondary hover:text-default-primary border border-elevation-subtle-soft",
                )}
                onClick={handleSelectAll}
              >
                {t_i18n(fn ? "All functions" : "All tools")}
              </button>
              <button
                className={cn(
                  "rounded-sm px-2.5 py-1 text-[11px] font-medium transition-colors",
                  !allTools && selected.size === 0
                    ? "bg-feedback-error-primary/10 text-feedback-error-primary border border-feedback-error-primary/20"
                    : "bg-elevation-hover text-default-secondary hover:text-default-primary border border-elevation-subtle-soft",
                )}
                onClick={handleDeselectAll}
              >
                {t_i18n("None")}
              </button>
              {search.trim() && filteredTools.length > 0 && (
                <>
                  <button
                    className="rounded-sm px-2.5 py-1 text-[11px] font-medium transition-colors bg-elevation-hover text-default-secondary hover:text-accent border border-elevation-subtle-soft"
                    onClick={handleSelectMatching}
                  >
                    {t_i18n("Select matching")}
                  </button>
                  <button
                    className="rounded-sm px-2.5 py-1 text-[11px] font-medium transition-colors bg-elevation-hover text-default-secondary hover:text-default-primary border border-elevation-subtle-soft"
                    onClick={handleDeselectMatching}
                  >
                    {t_i18n("Deselect matching")}
                  </button>
                </>
              )}
            </div>
            <span className="content-caption text-default-secondary shrink-0">
              {search.trim()
                ? t_i18n("{selected} of {total} matching selected", { selected: selectedInFiltered, total: filteredTools.length })
                : allTools
                  ? t_i18n(fn ? "{count, plural, one {# function} other {# functions}} (all)" : "{count} tools (all)", { count: tools.length })
                  : fn
                    ? t_i18n("{selected} of {count, plural, one {# function} other {# functions}} selected", { selected: selected.size, count: tools.length })
                    : t_i18n("{selected} of {total} selected", { selected: selected.size, total: tools.length })}
            </span>
          </div>

          {/* Tool list */}
          <div className="max-h-[350px] overflow-y-auto space-y-1 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2">
            {isLoading ? (
              <div className="flex items-center justify-center py-8">
                <Spinner size="md" />
                <span className="ml-2 content-base text-default-secondary">{t_i18n(fn ? "Loading functions…" : "Discovering tools...")}</span>
              </div>
            ) : filteredTools.length > 0 ? (
              filteredTools.map((tool) => {
                const isEnabled = allTools || selected.has(tool.name);
                return (
                  <button
                    key={tool.name}
                    className={cn(
                      "flex items-start gap-3 rounded-sm px-3 py-2.5 text-left w-full transition-all",
                      isEnabled
                        ? "bg-accent/5 border border-accent/20"
                        : "bg-elevation-hover border border-transparent hover:border-elevation-subtle-soft",
                    )}
                    onClick={() => toggleTool(tool.name)}
                  >
                    <div className={cn(
                      "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-sm border transition-colors",
                      isEnabled ? "bg-accent border-accent" : "border-elevation-subtle-soft",
                    )}>
                      {isEnabled && <Check className="h-3 w-3 text-negative-primary" />}
                    </div>
                    <div className="min-w-0 flex-1">
                      {tool.label ? (
                        <>
                          <p className={cn("content-compact font-medium", isEnabled ? "text-default-primary" : "text-default-secondary")}>{tool.label}</p>
                          <p className="content-caption font-mono-plex text-default-secondary">{tool.name}</p>
                        </>
                      ) : (
                        <p className={cn("content-compact font-medium font-mono-plex", isEnabled ? "text-default-primary" : "text-default-secondary")}>{tool.name}</p>
                      )}
                      {tool.description && (
                        <p className="content-caption text-default-secondary mt-0.5 line-clamp-2">{tool.description}</p>
                      )}
                    </div>
                  </button>
                );
              })
            ) : tools.length > 0 ? (
              <div className="py-6 text-center">
                <p className="content-base text-default-secondary">{t_i18n(fn ? "No functions match \u201C{search}\u201D" : "No tools match \u201C{search}\u201D", { search })}</p>
              </div>
            ) : (
              <div className="py-6 text-center">
                <Wrench className="h-6 w-6 text-default-disabled mx-auto mb-1.5" />
                <p className="content-base text-default-secondary">{t_i18n(fn ? "No functions available" : "No tools discovered")}</p>
                <p className="content-caption text-default-secondary mt-0.5">
                  {t_i18n(fn ? "This {sourceType} has no functions available." : "This {sourceType} has no tools available.", { sourceType: t_i18n(sourceType) })}
                </p>
              </div>
            )}
          </div>

          {/* Warning when no tools selected */}
          {!allTools && selected.size === 0 && tools.length > 0 && (
            <p data-testid="tool-scope-none-warning" className={cn("content-caption", TOOL_SCOPE_NOTE_TEXT)}>{t_i18n(fn ? "No functions selected: the {contextLabel} will not be able to use this {sourceType}." : "No tools selected: the {contextLabel} will not be able to use this {sourceType}.", { contextLabel: t_i18n(contextLabel), sourceType: t_i18n(sourceType) })}</p>
          )}

          {/* Actions */}
          <div className="flex justify-end gap-2 pt-1">
            <Button variant="outline" size="sm" onClick={onClose}>{t_i18n("Cancel")}</Button>
            <Button size="sm" onClick={handleSave} className="gap-1.5">
              <Save className="h-3.5 w-3.5" /> {t_i18n("Apply")}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
