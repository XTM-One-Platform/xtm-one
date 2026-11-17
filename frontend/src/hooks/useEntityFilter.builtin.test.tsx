// The built-in fold of useEntityFilter (Agents, Fleets; #11831): landing,
// counts, the Show/Hide choice, its URL parameter and its stored value.

import type { ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { MemoryRouter, useSearchParams } from "react-router-dom";

import { useEntityFilter } from "./useEntityFilter";
import { builtinStorageKey } from "@/lib/builtin-fold";

type Row = { id: string; name: string; description: null; user_id: string | null; is_company_managed: boolean; is_builtin: boolean };
const row = (id: string, over: Partial<Row> = {}): Row => ({
  id, name: id, description: null, user_id: null, is_company_managed: true, is_builtin: false, ...over,
});
const FOLD = { list: "agents", isBuiltin: (r: Row) => r.is_builtin };

const CUSTOM = row("custom");
const BUILTINS = [row("catalog-a", { is_builtin: true }), row("catalog-b", { is_builtin: true })];
const MINE = row("mine", { is_company_managed: false, user_id: "u1" });

function mount(url: string, items: Row[]) {
  const wrapper = ({ children }: { children: ReactNode }) => <MemoryRouter initialEntries={[url]}>{children}</MemoryRouter>;
  return renderHook(
    () => ({ filter: useEntityFilter({ items, userId: "u1", builtin: FOLD }), params: useSearchParams()[0] }),
    { wrapper },
  );
}

afterEach(() => localStorage.clear());

describe("useEntityFilter built-in fold", () => {
  it("lands on Mine first when Mine holds items, even with company items of the organisation's own", () => {
    const hook = mount("/agents", [MINE, CUSTOM, ...BUILTINS]);
    expect(hook.result.current.filter.visibilityFilter).toBe("mine");
    expect(hook.result.current.filter.builtinFold).toMatchObject({ choice: "hide" });
  });

  it("lands on Company with built-ins hidden when Mine is empty and the organisation has its own company items", () => {
    const hook = mount("/agents", [CUSTOM, ...BUILTINS]);
    const { filter, params } = hook.result.current;
    expect(filter.visibilityFilter).toBe("company");
    expect(params.get("vis")).toBe("company");
    expect(filter.filtered.map((r) => r.id)).toEqual(["custom"]);
    expect(filter.visibilityCounts.company).toBe(1);
    expect(filter.visibilityCounts.all).toBe(3);
    expect(filter.builtinFold).toMatchObject({ choice: "hide", hidden: 2, builtinInCompany: 2 });
  });

  it("lands on Company with built-ins shown on a fresh install, nothing hidden", () => {
    const hook = mount("/agents", BUILTINS);
    const { filter } = hook.result.current;
    expect(filter.visibilityFilter).toBe("company");
    expect(filter.filtered).toHaveLength(2);
    expect(filter.builtinFold).toMatchObject({ choice: "show", hidden: 0 });
  });

  it("lands on Company with built-ins shown before Shared when Mine and the custom Company are empty", () => {
    const SHARED = row("shared", { is_company_managed: false, user_id: "u2" });
    const hook = mount("/agents", [SHARED, ...BUILTINS]);
    const { filter } = hook.result.current;
    expect(filter.visibilityFilter).toBe("company");
    expect(filter.builtinFold).toMatchObject({ choice: "show", hidden: 0 });
  });

  it("lands on Mine when Company holds built-ins only", () => {
    const hook = mount("/agents", [MINE, ...BUILTINS]);
    expect(hook.result.current.filter.visibilityFilter).toBe("mine");
  });

  it("Show writes ?builtin=show, stores it, and the next mount reads it", () => {
    const hook = mount("/agents", [CUSTOM, ...BUILTINS]);
    act(() => hook.result.current.filter.builtinFold?.setChoice("show"));
    expect(hook.result.current.params.get("builtin")).toBe("show");
    expect(hook.result.current.filter.filtered).toHaveLength(3);
    expect(hook.result.current.filter.visibilityCounts.company).toBe(3);
    expect(localStorage.getItem(builtinStorageKey("agents", "u1"))).toBe("show");
    const again = mount("/agents", [CUSTOM, ...BUILTINS]);
    expect(again.result.current.filter.builtinFold?.choice).toBe("show");
  });

  it("an explicit ?builtin= and ?vis= win over the stored choice and the landing", () => {
    localStorage.setItem(builtinStorageKey("agents", "u1"), "show");
    const hook = mount("/agents?vis=all&builtin=hide", [MINE, CUSTOM, ...BUILTINS]);
    expect(hook.result.current.filter.visibilityFilter).toBe("all");
    expect(hook.result.current.filter.builtinFold?.choice).toBe("hide");
    // All keeps everything, built-ins included.
    expect(hook.result.current.filter.filtered).toHaveLength(4);
  });

  it("a search matching built-ins only lists them with the hint", () => {
    const hook = mount("/agents?q=catalog", [CUSTOM, ...BUILTINS]);
    expect(hook.result.current.filter.filtered.map((r) => r.id)).toEqual(["catalog-a", "catalog-b"]);
    expect(hook.result.current.filter.builtinFold?.revealedBySearch).toBe(true);
    // Company counts the rows shown, never 0 over a list of built-ins.
    expect(hook.result.current.filter.visibilityCounts.company).toBe(2);
  });

  it("a list without the option is unchanged", () => {
    const wrapper = ({ children }: { children: ReactNode }) => <MemoryRouter initialEntries={["/x"]}>{children}</MemoryRouter>;
    const hook = renderHook(() => useEntityFilter({ items: [MINE, CUSTOM, ...BUILTINS], userId: "u1" }), { wrapper });
    expect(hook.result.current.visibilityFilter).toBe("mine");
    expect(hook.result.current.builtinFold).toBeNull();
    expect(hook.result.current.visibilityCounts.company).toBe(3);
  });
});
