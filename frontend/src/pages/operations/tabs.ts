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
 * The Operations tabs, in order. A plain module so the list is pinned by
 * `tabs.test.tsx`: a surface that wants a new tab here has to say so there.
 * Scheduled work items are work items, listed by the `Work Items`
 * tab's `Scheduled` filter, not a tab of their own.
 */
import {
  Database,
  History,
  Layers,
  LayoutGrid,
  Library,
  ListChecks,
} from "lucide-react";

export type OpsTab =
  | "executions"
  | "background-tasks"
  | "work-items"
  | "state"
  | "worksets"
  | "workspaces";

export const OPERATIONS_TABS: { key: OpsTab; label: string; icon: React.ElementType }[] = [
  { key: "executions", label: "Executions", icon: History },
  { key: "background-tasks", label: "Background Tasks", icon: Layers },
  { key: "work-items", label: "Work Items", icon: ListChecks },
  { key: "state", label: "Shared State", icon: Database },
  { key: "worksets", label: "Worksets", icon: Library },
  // Last, and deliberately so: a workspace is the boundary the four tabs
  // above are scoped by, so it reads as "and here is how these are grouped"
  // rather than as another kind of work.
  { key: "workspaces", label: "Workspaces", icon: LayoutGrid },
];

/**
 * The filters a tab link carries from the current URL: the fleet a
 * reader picked stays selected when they move between the lists, as on the
 * fleet page's Operations view where it is locked. `agent` goes only to the
 * tabs that filter by agent. Page, search and status stay per tab.
 * Workspaces has no fleet filter, yet its link still carries `fleet`:
 * the tab ignores it, and the next tab switch reads it back from the URL, so a
 * visit to Workspaces does not drop the fleet the reader picked.
 */
const OPERATIONS_CARRIED_PARAMS: Record<OpsTab, readonly string[]> = {
  executions: ["fleet", "agent"],
  "background-tasks": ["fleet"],
  "work-items": ["fleet", "agent"],
  state: ["fleet"],
  worksets: ["fleet"],
  workspaces: ["fleet"],
};

/** The query string (with its `?`, or empty) of the link to `key`. */
export function operationsTabSearch(key: OpsTab, current: URLSearchParams): string {
  const carried = new URLSearchParams();
  for (const name of OPERATIONS_CARRIED_PARAMS[key]) {
    const value = current.get(name);
    if (value) carried.set(name, value);
  }
  const query = carried.toString();
  return query ? `?${query}` : "";
}
