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
 * The Operations and Tasks tabs, in order (#775). A plain module so each
 * list is pinned by `tabs.test.tsx`: a surface that wants a new tab here has
 * to say so there. Operations holds what is running or past; Tasks what to
 * do and follow. Scheduled work items are work items, listed by the `Work
 * Items` tab's `Scheduled` filter, not a tab of their own.
 */
import {
  Database,
  History,
  Layers,
  LayoutGrid,
  Library,
  ListChecks,
  TrendingUp,
} from "lucide-react";
import {
  OPERATIONS_PATH, TASKS_PATH, WORKSPACES_PATH, operationsPath, valuePath, workItemsPath, type OperationsTab, type TasksTab,
} from "@/lib/task-routes";

type OpsTab = OperationsTab;

/** Each tab is its own route (`/operations/<key>`); `/operations` alone is Executions. */
export const OPERATIONS_TABS: { key: OpsTab; label: string; icon: React.ElementType; to: string }[] = [
  { key: "executions", label: "Executions", icon: History, to: operationsPath("executions") },
  { key: "background-tasks", label: "Background Tasks", icon: Layers, to: operationsPath("background-tasks") },
  { key: "shared-state", label: "Shared State", icon: Database, to: operationsPath("shared-state") },
  { key: "worksets", label: "Worksets", icon: Library, to: operationsPath("worksets") },
];

/** The tab an Operations path shows: `/operations` alone is Executions. */
export function operationsTabOf(pathname: string): OpsTab {
  const segment = pathname.slice(OPERATIONS_PATH.length).split("/")[1] ?? "";
  return OPERATIONS_TABS.find((tab) => tab.key === segment)?.key ?? "executions";
}

/**
 * The filters a tab link carries from the current URL: the fleet a
 * reader picked stays selected when they move between the lists, as on the
 * fleet page's Operations view where it is locked. `agent` goes only to the
 * tabs that filter by agent. Page, search and status stay per tab.
 */
const OPERATIONS_CARRIED_PARAMS: Record<OpsTab, readonly string[]> = {
  executions: ["fleet", "agent"],
  "background-tasks": ["fleet"],
  "shared-state": ["fleet"],
  worksets: ["fleet"],
};

function carried(names: readonly string[], current: URLSearchParams): string {
  const params = new URLSearchParams();
  for (const name of names) {
    const value = current.get(name);
    if (value) params.set(name, value);
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

/** The query string (with its `?`, or empty) of the link to `key`. */
export function operationsTabSearch(key: OpsTab, current: URLSearchParams): string {
  return carried(OPERATIONS_CARRIED_PARAMS[key], current);
}

/** The Tasks tabs: each its own route, Work Items first and the default. */
export const TASKS_TABS: { key: TasksTab; label: string; icon: React.ElementType; to: string }[] = [
  { key: "work-items", label: "Work Items", icon: ListChecks, to: workItemsPath() },
  { key: "value", label: "Value", icon: TrendingUp, to: valuePath() },
  // Last: a workspace is the boundary work items are filed in.
  { key: "workspaces", label: "Workspaces", icon: LayoutGrid, to: WORKSPACES_PATH },
];

/** The tab a Tasks path shows: `/tasks` alone is Work Items. */
export function tasksTabOf(pathname: string): TasksTab {
  const segment = pathname.slice(TASKS_PATH.length).split("/")[1] ?? "";
  return segment === "value" || segment === "workspaces" ? segment : "work-items";
}

/**
 * The filters a Tasks tab link carries: the fleet stays selected across the
 * lists (Workspaces ignores it but keeps it in the URL), the agent goes to Work
 * Items only, the Value tab keeps nothing (its scope and window are its own).
 */
export function tasksTabSearch(key: TasksTab, current: URLSearchParams): string {
  if (key === "value") return "";
  return carried(key === "work-items" ? ["fleet", "agent"] : ["fleet"], current);
}
