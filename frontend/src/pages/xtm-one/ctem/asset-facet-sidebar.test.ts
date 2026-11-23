// SPDX-License-Identifier: Apache-2.0

/** The URL writes of the asset facet sidebar (#1501): the keys the table reads, back on the first page. */
import { describe, expect, test } from "vitest";
import { clearFacetParams, facetParam, toggleFacetParam } from "./AssetFacetSidebar";

describe("asset facet URL state", () => {
  test("names a facet under the table's prefix, so ?a_f_category=ai_target keeps working", () => {
    expect(facetParam("a_", "category")).toBe("a_f_category");
  });

  test("adds then removes a value (OR within a facet) and goes back to the first page", () => {
    const start = new URLSearchParams("a_f_category=host&a_page=3&t_q=nginx");
    const added = toggleFacetParam(start, "a_", "category", "ai_target");
    expect(added.getAll("a_f_category")).toEqual(["host", "ai_target"]);
    expect(added.get("a_page")).toBeNull();
    expect(added.get("t_q")).toBe("nginx");
    const removed = toggleFacetParam(added, "a_", "category", "host");
    expect(removed.getAll("a_f_category")).toEqual(["ai_target"]);
  });

  test("clears the facets named and keeps the others and the search", () => {
    const start = new URLSearchParams("a_f_category=host&a_f_subcategory=server&a_f_source=intune&a_q=web");
    const cleared = clearFacetParams(start, "a_", ["category", "subcategory"]);
    expect(cleared.getAll("a_f_category")).toEqual([]);
    expect(cleared.getAll("a_f_subcategory")).toEqual([]);
    expect(cleared.getAll("a_f_source")).toEqual(["intune"]);
    expect(cleared.get("a_q")).toBe("web");
  });
});
