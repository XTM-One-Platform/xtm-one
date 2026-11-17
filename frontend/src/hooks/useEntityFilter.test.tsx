// A filter change and the reset of a paged list's page are one navigation:
// two setSearchParams calls in one event start from the same query, and the
// second drops the filter.

import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { MemoryRouter, useSearchParams } from "react-router-dom";

import { useEntityFilter } from "./useEntityFilter";

const ITEMS = [{ id: "a", name: "Alpha", description: null, user_id: "u1", is_company_managed: false }];
const RESET = ["page"] as const;

function mount(url: string, resetParams?: readonly string[]) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={[url]}>{children}</MemoryRouter>
  );
  return renderHook(
    () => ({ filter: useEntityFilter({ items: ITEMS, userId: "u1", resetParams }), params: useSearchParams()[0] }),
    { wrapper },
  );
}

describe("useEntityFilter resetParams", () => {
  it("writes the search and drops the page in one navigation", () => {
    const hook = mount("/fleets?page=3&ws=w1", RESET);
    act(() => hook.result.current.filter.setSearch("soc"));
    expect(hook.result.current.params.get("q")).toBe("soc");
    expect(hook.result.current.params.get("page")).toBeNull();
    expect(hook.result.current.params.get("ws")).toBe("w1");
  });

  it("drops the page on a visibility or has: change too", () => {
    const hook = mount("/fleets?page=2", RESET);
    act(() => hook.result.current.filter.setVisibilityFilter("mine"));
    expect(hook.result.current.params.toString()).toBe("vis=mine");
    act(() => hook.result.current.filter.setSelectedProperties(["members"]));
    expect(hook.result.current.params.get("has")).toBe("members");
    expect(hook.result.current.params.get("vis")).toBe("mine");
  });

  it("clearAll writes the landed segment and drops the search, has: and the page in one navigation", () => {
    // The page lands on Mine (it holds Alpha), then the user narrows it to an empty view.
    const hook = mount("/fleets?ws=w1", RESET);
    expect(hook.result.current.filter.landingVisibilityFilter).toBe("mine");
    act(() => hook.result.current.filter.setVisibilityFilter("shared"));
    act(() => hook.result.current.filter.setSearch("zzz"));
    act(() => hook.result.current.filter.setSelectedProperties(["members"]));
    act(() => hook.result.current.filter.setSelectedTags(["t"]));
    expect(hook.result.current.params.get("q")).toBe("zzz");
    expect(hook.result.current.filter.visibilityFilter).toBe("shared");
    act(() =>
      hook.result.current.filter.clearAll((next) => {
        next.delete("ws");
        return next;
      }),
    );
    // The URL names the view shown: the landing segment, nothing else.
    expect(hook.result.current.params.toString()).toBe("vis=mine");
    expect(hook.result.current.params.get("vis")).toBe(hook.result.current.filter.visibilityFilter);
    expect(hook.result.current.filter.visibilityFilter).toBe("mine");
    expect(hook.result.current.filter.search).toBe("");
    expect(hook.result.current.filter.selectedProperties).toEqual([]);
    expect(hook.result.current.filter.selectedTags).toEqual([]);
  });

  it("clearAll with keepScope keeps the segment shown: the scope is not a filter", () => {
    // Standard v2 (UX round 1 on #10920, B2): clearing never lowers the total.
    const hook = mount("/fleets?ws=w1", RESET);
    act(() => hook.result.current.filter.setVisibilityFilter("all"));
    act(() => hook.result.current.filter.setSearch("zzz"));
    act(() => hook.result.current.filter.setSelectedTags(["t"]));
    act(() => hook.result.current.filter.clearAll(undefined, { keepScope: true }));
    expect(hook.result.current.filter.visibilityFilter).toBe("all");
    expect(hook.result.current.params.get("vis")).toBe("all");
    expect(hook.result.current.params.get("q")).toBeNull();
    expect(hook.result.current.filter.search).toBe("");
    expect(hook.result.current.filter.selectedTags).toEqual([]);
  });

  it("a segment click and a keystroke before the next render both reach the URL", () => {
    const hook = mount("/fleets?ws=w1&page=2", RESET);
    expect(hook.result.current.params.get("vis")).toBe("mine");
    // Both writes start from the same render: the second must not undo the first.
    act(() => {
      hook.result.current.filter.setVisibilityFilter("shared");
      hook.result.current.filter.setSearch("zzz");
    });
    expect(hook.result.current.params.toString()).toBe("ws=w1&vis=shared&q=zzz");
    expect(hook.result.current.filter.visibilityFilter).toBe("shared");
    expect(hook.result.current.filter.search).toBe("zzz");
    // A later write still starts from the rendered URL.
    act(() => hook.result.current.filter.setSelectedProperties(["members"]));
    expect(hook.result.current.params.toString()).toBe("ws=w1&vis=shared&q=zzz&has=members");
  });

  it("a page write in the same tick as a keystroke starts from the latest query", () => {
    const hook = mount("/fleets?vis=mine&page=1", RESET);
    act(() => {
      hook.result.current.filter.setSearch("x");
      // The page clamp: before the fix it resolved against `vis=mine&page=1` and dropped `q`.
      hook.result.current.filter.writeParams((prev) => {
        const next = new URLSearchParams(prev);
        next.delete("page");
        return next;
      });
    });
    expect(hook.result.current.params.toString()).toBe("vis=mine&q=x");
  });

  it("lands from the counts before the search and has: filters, so clearAll goes back to Mine", () => {
    // Opened with a segment and a search matching nothing: every narrowed count is 0.
    const hook = mount("/fleets?vis=shared&q=zzz&has=members");
    expect(hook.result.current.filter.visibilityCounts.all).toBe(0);
    expect(hook.result.current.filter.visibilityFilter).toBe("shared");
    expect(hook.result.current.filter.landingVisibilityFilter).toBe("mine");
    act(() => hook.result.current.filter.clearAll());
    expect(hook.result.current.params.toString()).toBe("vis=mine");
    expect(hook.result.current.filter.visibilityFilter).toBe("mine");
    expect(hook.result.current.filter.filtered.map((row) => row.name)).toEqual(["Alpha"]);
  });

  it("keeps every other parameter when no reset is asked for", () => {
    const hook = mount("/agents?page=2");
    act(() => hook.result.current.filter.setSearch("x"));
    expect(hook.result.current.params.get("page")).toBe("2");
  });
});

const MIXED = [
  { id: "a", name: "Alpha", description: null, user_id: "u2", is_company_managed: true },
  { id: "b", name: "Bravo", description: null, user_id: "u2", is_company_managed: false, shared_group_ids: ["g"] },
];

function mountMixed(url: string, withVisibility?: boolean) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={[url]}>{children}</MemoryRouter>
  );
  return renderHook(() => useEntityFilter({ items: MIXED, userId: "u1", withVisibility }), { wrapper });
}

describe("useEntityFilter visibility", () => {
  it("counts the segments under the search, so a search matching nothing empties every segment", () => {
    const hook = mountMixed("/agents");
    expect(hook.result.current.visibilityCounts).toEqual({ mine: 0, shared: 1, company: 1, all: 2 });
    act(() => hook.result.current.setSearch("zulu"));
    expect(hook.result.current.visibilityCounts).toEqual({ mine: 0, shared: 0, company: 0, all: 0 });
  });

  it("with no segment in the URL, lands on Company when Mine is empty and writes it to ?vis=", () => {
    const wrapper = ({ children }: { children: ReactNode }) => (
      <MemoryRouter initialEntries={["/agents?page=2"]}>{children}</MemoryRouter>
    );
    const hook = renderHook(
      () => ({ filter: useEntityFilter({ items: MIXED, userId: "u1", resetParams: RESET }), params: useSearchParams()[0] }),
      { wrapper },
    );
    expect(hook.result.current.filter.visibilityFilter).toBe("company");
    expect(hook.result.current.filter.filtered.map((row) => row.id)).toEqual(["a"]);
    expect(hook.result.current.params.get("vis")).toBe("company");
    // Landing is not a filter change: the link's page is kept.
    expect(hook.result.current.params.get("page")).toBe("2");
    // A later search never moves the landed segment.
    act(() => hook.result.current.filter.setSearch("bravo"));
    expect(hook.result.current.filter.visibilityFilter).toBe("company");
  });

  it("lands on Shared when Mine and Company are empty, on Mine when it holds an item", () => {
    const sharedOnly = [MIXED[1]];
    const wrapper = ({ children }: { children: ReactNode }) => <MemoryRouter initialEntries={["/agents"]}>{children}</MemoryRouter>;
    const shared = renderHook(() => useEntityFilter({ items: sharedOnly, userId: "u1" }), { wrapper });
    expect(shared.result.current.visibilityFilter).toBe("shared");
    const own = renderHook(() => useEntityFilter({ items: [...MIXED, ...ITEMS], userId: "u1" }), { wrapper });
    expect(own.result.current.visibilityFilter).toBe("mine");
  });

  it("waits for the list before landing, then lands from its counts", () => {
    const wrapper = ({ children }: { children: ReactNode }) => <MemoryRouter initialEntries={["/agents"]}>{children}</MemoryRouter>;
    const hook = renderHook(
      ({ items }: { items: typeof MIXED | undefined }) => ({ filter: useEntityFilter({ items, userId: "u1" }), params: useSearchParams()[0] }),
      { wrapper, initialProps: { items: undefined as typeof MIXED | undefined } },
    );
    expect(hook.result.current.params.has("vis")).toBe(false);
    hook.rerender({ items: MIXED });
    expect(hook.result.current.filter.visibilityFilter).toBe("company");
    expect(hook.result.current.params.get("vis")).toBe("company");
  });

  it("a loading list, then one owned item, lands on Mine (the Fleets case)", () => {
    const wrapper = ({ children }: { children: ReactNode }) => <MemoryRouter initialEntries={["/fleets"]}>{children}</MemoryRouter>;
    const hook = renderHook(
      ({ items }: { items: typeof ITEMS | undefined }) => ({ filter: useEntityFilter({ items, userId: "u1" }), params: useSearchParams()[0] }),
      { wrapper, initialProps: { items: undefined as typeof ITEMS | undefined } },
    );
    expect(hook.result.current.params.has("vis")).toBe(false);
    hook.rerender({ items: ITEMS });
    expect(hook.result.current.filter.visibilityFilter).toBe("mine");
    expect(hook.result.current.params.get("vis")).toBe("mine");
    expect(hook.result.current.filter.landingVisibilityFilter).toBe("mine");
  });

  it("an explicit ?vis= or alias always wins, an empty Mine included", () => {
    for (const [url, expected] of [["/agents?vis=mine", "mine"], ["/agents?visibility=shared", "shared"], ["/agents?ownership=shared", "all"]] as const) {
      const hook = mountMixed(url);
      expect(hook.result.current.visibilityFilter).toBe(expected);
    }
  });

  it("a page with no control shows every row and ignores ?vis= and its aliases", () => {
    for (const url of ["/datasets?vis=mine", "/datasets?visibility=mine", "/datasets?ownership=shared"]) {
      const hook = mountMixed(url, false);
      expect(hook.result.current.visibilityFilter).toBe("all");
      expect(hook.result.current.filtered.map((row) => row.id)).toEqual(["a", "b"]);
    }
  });
});
