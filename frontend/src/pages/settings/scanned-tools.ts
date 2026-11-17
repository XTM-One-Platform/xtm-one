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
 * "Tools screened by the prompt guard": pure helpers of its editor.
 *
 * The option stores entries (`prompt_guard_scanned_tools`): a tool's
 * registered name, or `custom:<id>` for a custom tool, whose owner may rename
 * it. The editor picks them from the tools the platform can screen
 * (`GET /settings/security-options/scannable-tools`), so an entry is never a
 * typo that screens nothing, shows a custom tool by its current name, and
 * flags an entry no tool carries anymore (a deleted custom tool) so the
 * administrator can remove it.
 */
import type { ScannableTool, ScannableToolGroup } from "@/types";

/** The prefix of a custom tool's entry (`CUSTOM_TOOL_SCAN_PREFIX` in `app/core/config.py`). */
export const CUSTOM_TOOL_ENTRY_PREFIX = "custom:";

interface ScannableCandidate {
  tool: ScannableTool;
  group: ScannableToolGroup;
}

/** How many matches the search lists at once: the catalogue holds thousands of tools. */
export const SCANNABLE_SEARCH_LIMIT = 50;

/** Every candidate by its entry; the first group that offers an entry names it. */
export function indexCandidates(groups: readonly ScannableToolGroup[]): Map<string, ScannableCandidate> {
  const index = new Map<string, ScannableCandidate>();
  for (const group of groups) {
    for (const tool of group.tools) {
      if (!index.has(tool.entry)) index.set(tool.entry, { tool, group });
    }
  }
  return index;
}

/** An entry as the editor shows it: its tool's current name, or the entry itself when no tool carries it. */
export interface DescribedEntry {
  entry: string;
  name: string;
  group: string | null;
  /** No tool offers it anymore (a deleted tool, a disconnected server). */
  stale: boolean;
  /** A custom tool's entry (`custom:<id>`): named by the tool, never by the entry. */
  custom: boolean;
}

export function isCustomToolEntry(entry: string): boolean {
  return entry.startsWith(CUSTOM_TOOL_ENTRY_PREFIX);
}

export function describeEntry(entry: string, index: ReadonlyMap<string, ScannableCandidate> | null): DescribedEntry {
  const custom = isCustomToolEntry(entry);
  const candidate = index?.get(entry);
  if (candidate) return { entry, name: candidate.tool.name, group: candidate.group.label, stale: false, custom };
  // Unknown while the list is loading: not flagged until it says so.
  return { entry, name: entry, group: null, stale: index !== null, custom };
}

/** The candidates matching *query* (name, entry, group or description), at most *limit*. */
export function searchCandidates(
  groups: readonly ScannableToolGroup[],
  query: string,
  limit: number = SCANNABLE_SEARCH_LIMIT,
): { matches: ScannableCandidate[]; total: number } {
  const needle = query.trim().toLowerCase();
  if (!needle) return { matches: [], total: 0 };
  const matches: ScannableCandidate[] = [];
  let total = 0;
  const seen = new Set<string>();
  for (const group of groups) {
    const groupMatches = group.label.toLowerCase().includes(needle);
    for (const tool of group.tools) {
      if (seen.has(tool.entry)) continue;
      const hit =
        groupMatches ||
        tool.name.toLowerCase().includes(needle) ||
        tool.entry.toLowerCase().includes(needle) ||
        tool.description.toLowerCase().includes(needle);
      if (!hit) continue;
      seen.add(tool.entry);
      total += 1;
      if (matches.length < limit) matches.push({ tool, group });
    }
  }
  return { matches, total };
}

/** *entries* with *entry* added or removed: sorted and each once, as the platform stores them. */
export function toggleEntry(entries: readonly string[], entry: string, include: boolean): string[] {
  const next = new Set(entries);
  if (include) next.add(entry);
  else next.delete(entry);
  return [...next].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}
