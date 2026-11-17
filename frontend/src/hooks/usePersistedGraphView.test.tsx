/**
 * The persisted graph view: a change shows at once and is never
 * replaced by the previous view while the router commits the new query, so a
 * quick second toggle reads the view on screen.
 */
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { usePersistedGraphView } from "@/hooks/usePersistedGraphView";
import { MAP_VIEW_DEFAULTS, type GraphViewState } from "@/lib/agent-graph-view-state";

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: { id: "u-1" } }) }));

const wrapper = ({ children }: { children: ReactNode }) => (
  <MemoryRouter initialEntries={["/flows/map/schedule?vis=all"]}>{children}</MemoryRouter>
);

function useViewAndQuery() {
  const [view, update] = usePersistedGraphView("map", MAP_VIEW_DEFAULTS);
  const { search } = useLocation();
  return { view, update, search };
}

describe("usePersistedGraphView", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    window.localStorage.clear();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("never shows the previous filters between the write and the router's commit", () => {
    const seen: GraphViewState["filters"]["kinds"][] = [];
    const { result } = renderHook(
      () => {
        const out = useViewAndQuery();
        seen.push(out.view.filters.kinds);
        return out;
      },
      { wrapper },
    );
    const on: GraphViewState["filters"] = { ...MAP_VIEW_DEFAULTS.filters, kinds: ["builtin"] };

    act(() => result.current.update({ filters: on }));
    expect(result.current.view.filters.kinds).toEqual(["builtin"]);
    seen.length = 0;
    act(() => {
      vi.advanceTimersByTime(200);
    });

    expect(new URLSearchParams(result.current.search).get("kinds")).toBe("builtin");
    expect(result.current.view.filters.kinds).toEqual(["builtin"]);
    // Every render after the change showed it, the commit of the URL included.
    expect(seen.every((kinds) => kinds.length === 1 && kinds[0] === "builtin")).toBe(true);
  });

  it("turns a filter off again on a second toggle right after the first one is written", () => {
    const { result } = renderHook(useViewAndQuery, { wrapper });
    const on: GraphViewState["filters"] = { ...MAP_VIEW_DEFAULTS.filters, kinds: ["builtin"] };

    act(() => result.current.update({ filters: on }));
    act(() => {
      vi.advanceTimersByTime(200);
    });
    // The second toggle reads the view on screen, as the switch does.
    act(() => {
      const { kinds } = result.current.view.filters;
      result.current.update({ filters: { ...result.current.view.filters, kinds: kinds.filter((k) => k !== "builtin") } });
    });
    act(() => {
      vi.advanceTimersByTime(200);
    });

    expect(result.current.view.filters.kinds).toEqual([]);
    expect(new URLSearchParams(result.current.search).has("kinds")).toBe(false);
  });

  it("keeps a written change on screen and in the URL", () => {
    const { result } = renderHook(useViewAndQuery, { wrapper });
    act(() => result.current.update({ layout: "hierarchy-lr" }));
    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(result.current.view.layout).toBe("hierarchy-lr");
    expect(new URLSearchParams(result.current.search).get("layout")).toBe("hierarchy-lr");
  });

  it("stores a layout picked on the toolbar as a choice, and another change without one", () => {
    const { result } = renderHook(useViewAndQuery, { wrapper });
    act(() => result.current.update({ fleetsLayer: false }));
    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(JSON.parse(window.localStorage.getItem("xtmone.graphView.u-1.map") ?? "{}").layoutChosen).toBe(false);
    act(() => result.current.update({ layout: "radial" }));
    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(JSON.parse(window.localStorage.getItem("xtmone.graphView.u-1.map") ?? "{}").layoutChosen).toBe(true);
  });
});
