// SPDX-License-Identifier: Apache-2.0

/**
 * Grid ⇄ list view preference for asset index pages.
 *
 * Every asset index (Agents, Tools, Variables, Prompts, Skills, MCP Servers,
 * Integrations, Knowledge) can render as a card grid or as a sortable table.
 * The choice is scoped per page and cached in localStorage: these are
 * long-lived operator surfaces, so re-picking the view on every visit — or
 * after every navigation — is pure friction.
 *
 * Not a URL param: unlike the filters in `useEntityFilter`, the view mode is a
 * durable personal preference rather than a shareable description of *what* is
 * on screen.
 */

import { useCallback, useState } from "react";

/**
 * `graph`: a page that also draws its records as a graph (Tasks > Work Items) offers it;
 * `kanban`: a page that also draws its records as status columns (CTEM Cases, #1503).
 */
export type ViewMode = "grid" | "list" | "graph" | "kanban";

const STORAGE_PREFIX = "copilot-view:";

function readStored(scope: string, fallback: ViewMode): ViewMode {
  try {
    const stored = localStorage.getItem(STORAGE_PREFIX + scope);
    return stored === "list" || stored === "grid" || stored === "graph" ? stored : fallback;
  } catch {
    // Private browsing / storage disabled — fall back to the page's default.
    return fallback;
  }
}

/**
 * @param scope Stable per-page key (e.g. `"agents"`, `"mcp-servers"`).
 * @param fallback The view before the reader chose one (default the card grid; a table page passes `"list"`).
 */
export function useViewMode(scope: string, fallback: ViewMode = "grid"): [ViewMode, (mode: ViewMode) => void] {
  const [viewMode, setViewModeState] = useState<ViewMode>(() => readStored(scope, fallback));

  const setViewMode = useCallback(
    (mode: ViewMode) => {
      setViewModeState(mode);
      try {
        localStorage.setItem(STORAGE_PREFIX + scope, mode);
      } catch { /* quota exceeded or private browsing */ }
    },
    [scope],
  );

  return [viewMode, setViewMode];
}
