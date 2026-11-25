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
import { AlertTriangle, RefreshCw, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SearchInput } from "@/components/ui/search-input";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { useScannableTools } from "@/hooks/api/useSettingsApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

import {
  CUSTOM_TOOL_ENTRY_PREFIX,
  describeEntry,
  indexCandidates,
  searchCandidates,
  toggleEntry,
  type DescribedEntry,
} from "./scanned-tools";
import { Spinner } from "@filigran/design-system";

// ---------------------------------------------------------------------------
// "Tools screened by the prompt guard": the tools are picked from the
// ones the platform can screen, never typed, so an entry is a name a tool
// carries; an entry no tool carries anymore is flagged for removal.
// ---------------------------------------------------------------------------

function EntryPill({ described, onRemove }: { described: DescribedEntry; onRemove?: () => void }) {
  const { t_i18n } = useFormatter();
  // A custom tool is listed by id; once deleted, its entry has no name left to show.
  const deletedCustomTool = described.custom && described.stale;
  const name = deletedCustomTool ? t_i18n("Deleted custom tool") : described.name;
  return (
    <li
      data-testid="scanned-tool"
      data-entry={described.entry}
      data-stale={described.stale || undefined}
      className={cn(
        "flex max-w-full items-center gap-1.5 rounded-sm border px-2 py-0.5 content-compact",
        described.stale ? "border-feedback-alert-primary/40 bg-feedback-alert-primary/10" : "border-elevation-subtle-soft bg-elevation-hover",
      )}
    >
      {described.stale && (
        <Tip
          content={
            deletedCustomTool
              ? t_i18n("This custom tool was deleted: remove it.")
              : t_i18n("No tool carries this entry anymore: remove it.")
          }
        >
          <AlertTriangle className="h-3 w-3 shrink-0 text-feedback-warning-primary light:text-feedback-warning-primary" aria-label={t_i18n("Not found")} />
        </Tip>
      )}
      <span className={cn("truncate", !deletedCustomTool && "font-mono-plex")}>{name}</span>
      {deletedCustomTool && (
        <span className="truncate font-mono-plex text-default-secondary">{described.entry.slice(CUSTOM_TOOL_ENTRY_PREFIX.length, CUSTOM_TOOL_ENTRY_PREFIX.length + 8)}</span>
      )}
      {described.group && <span className="truncate text-default-secondary">{described.group}</span>}
      {onRemove && (
        <Tip content={t_i18n("Remove")}>
          <Button
            variant="ghost"
            size="icon"
            className="h-4 w-4 shrink-0"
            aria-label={t_i18n("Remove {name}", { name })}
            onClick={onRemove}
          >
            <X className="h-3 w-3" />
          </Button>
        </Tip>
      )}
    </li>
  );
}

/** The listed tools, read-only, named as the platform names them now. */
export function ScannedToolsSummary({ entries }: { entries: string[] }) {
  const { t_i18n } = useFormatter();
  const query = useScannableTools(entries.length > 0);
  const index = useMemo(() => (query.data ? indexCandidates(query.data.groups) : null), [query.data]);
  if (entries.length === 0) {
    return (
      <Badge variant="outline" className="content-caption font-medium">
        {t_i18n("None")}
      </Badge>
    );
  }
  return (
    <ul className="flex flex-wrap gap-1.5" data-testid="scanned-tools-summary">
      {entries.map((entry) => (
        <EntryPill key={entry} described={describeEntry(entry, index)} />
      ))}
    </ul>
  );
}

/** The editor: the listed tools, removable, and a search over the tools that can be listed. */
export function ScannedToolsField({
  labelledBy,
  describedBy,
  value,
  onChange,
}: {
  labelledBy: string;
  describedBy: string;
  value: string[];
  onChange: (next: string[]) => void;
}) {
  const { t_i18n } = useFormatter();
  const query = useScannableTools(true);
  const [search, setSearch] = useState("");
  const groups = useMemo(() => query.data?.groups ?? [], [query.data]);
  const index = useMemo(() => (query.data ? indexCandidates(query.data.groups) : null), [query.data]);
  const { matches, total } = useMemo(() => searchCandidates(groups, search), [groups, search]);
  const listed = new Set(value);

  return (
    <div
      role="group"
      aria-labelledby={labelledBy}
      aria-describedby={describedBy}
      className="w-full space-y-3"
      data-testid="scanned-tools-field"
    >
      {value.length > 0 ? (
        <ul className="flex flex-wrap gap-1.5">
          {value.map((entry) => (
            <EntryPill
              key={entry}
              described={describeEntry(entry, index)}
              onRemove={() => onChange(toggleEntry(value, entry, false))}
            />
          ))}
        </ul>
      ) : (
        <p className="content-compact text-default-secondary">{t_i18n("No tool is screened.")}</p>
      )}

      {query.isPending ? (
        <p className="flex items-center gap-2 content-compact text-default-secondary" role="status">
          <Spinner size="sm" tone="inherit" />
          {t_i18n("Loading the tools...")}
        </p>
      ) : !query.data ? (
        <div className="flex items-center gap-2 content-compact text-feedback-error-primary">
          <span>{t_i18n("The tools could not be loaded.")}</span>
          <Button variant="ghost" size="sm" className="gap-1" onClick={() => void query.refetch()}>
            <RefreshCw className="h-3 w-3" />
            {t_i18n("Try again")}
          </Button>
        </div>
      ) : (
        <div className="space-y-2">
          <SearchInput
            value={search}
            onChange={setSearch}
            placeholder="Search a tool to screen..."
            className="max-w-none"
            inputClassName="h-9"
          />
          {search.trim() && (
            <>
              {matches.length === 0 ? (
                <p className="content-compact text-default-secondary">{t_i18n("No tool matches this search.")}</p>
              ) : (
                <ul className="max-h-72 space-y-1 overflow-y-auto rounded-sm border border-elevation-subtle-soft p-1">
                  {matches.map(({ tool, group }) => (
                    <li
                      key={tool.entry}
                      className="flex items-center gap-3 rounded-sm px-2 py-1.5 hover:bg-elevation-hover"
                      data-testid="scannable-tool"
                      data-entry={tool.entry}
                    >
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-baseline gap-x-2">
                          <span className="truncate font-mono-plex content-compact text-default-primary">{tool.name}</span>
                          <span className="truncate content-compact text-default-secondary">{group.label}</span>
                        </div>
                        {tool.description && (
                          <p className="truncate content-compact text-default-secondary">{tool.description}</p>
                        )}
                      </div>
                      <Switch
                        aria-label={t_i18n("Screen {name}", { name: tool.name })}
                        checked={listed.has(tool.entry)}
                        onCheckedChange={(checked) => onChange(toggleEntry(value, tool.entry, checked))}
                      />
                    </li>
                  ))}
                </ul>
              )}
              {total > matches.length && (
                <p className="content-compact text-default-secondary">
                  {t_i18n("{shown} of {total} matching tools shown: refine the search to see the others.", {
                    shown: matches.length,
                    total,
                  })}
                </p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
