// SPDX-License-Identifier: Apache-2.0

/**
 * The URIs of the Tasks and Operations pages (#775), the single place a link
 * to a work item, the Value report or a workspace is built. The paths mirror
 * the menu and its tabs exactly: Tasks holds what to do and follow (Work
 * Items, Value, Workspaces), Operations what is running or past (Executions,
 * Background Tasks, Shared State, Worksets). No other path serves them: a
 * link written anywhere else goes through these helpers, so the next move is
 * one edit here.
 */

/** The Tasks page; it opens on its first tab, Work Items. */
export const TASKS_PATH = "/tasks";
/** The Operations page; it opens on its first tab, Executions. */
export const OPERATIONS_PATH = "/operations";

export type TasksTab = "work-items" | "value" | "workspaces";
export type OperationsTab = "executions" | "background-tasks" | "shared-state" | "worksets";

/** A query string written with its `?`, from a string or a `URLSearchParams` (empty: none). */
function query(params?: string | URLSearchParams | Record<string, string>): string {
  if (!params) return "";
  const text = typeof params === "string" ? params.replace(/^\?/, "") : new URLSearchParams(params).toString();
  return text ? `?${text}` : "";
}

/** The Work Items list, with its filters (`view`, `agent`, `vis`...). */
export function workItemsPath(params?: string | URLSearchParams | Record<string, string>): string {
  return `${TASKS_PATH}/work-items${query(params)}`;
}

/** One work item open over the list: the decision card, the bell, a notification. */
export function workItemPath(id: string, params?: string | URLSearchParams | Record<string, string>): string {
  return `${TASKS_PATH}/work-items/${encodeURIComponent(id)}${query(params)}`;
}

/** The tables of the Value tab, its first one the default. */
type ValueTable = "fleets" | "agents" | "units";

/** The Value tab, on one of its tables (the first by default), with its scope and window. */
export function valuePath(table?: ValueTable, params?: string | URLSearchParams | Record<string, string>): string {
  return `${TASKS_PATH}/value${table && table !== "fleets" ? `/${table}` : ""}${query(params)}`;
}

/** How the value is computed, linked from every "How this is computed". */
export const VALUE_METHODOLOGY_PATH = `${TASKS_PATH}/value/methodology`;

/** The Workspaces list. */
export const WORKSPACES_PATH = `${TASKS_PATH}/workspaces`;

/** One workspace, on one of its tabs (its first by default). */
export function workspacePath(id: string, tab?: string): string {
  return `${WORKSPACES_PATH}/${encodeURIComponent(id)}${tab ? `/${tab}` : ""}`;
}

/** One tab of Operations; the shared state is `shared-state`, as its label. */
export function operationsPath(tab: OperationsTab, params?: string | URLSearchParams | Record<string, string>): string {
  return `${OPERATIONS_PATH}/${tab}${query(params)}`;
}
