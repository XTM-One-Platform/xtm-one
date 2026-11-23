// SPDX-License-Identifier: Apache-2.0

/** The URL writes of the asset facet sidebar (#1501): the keys the table reads, back on the first page. */
import { describe, expect, test } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { clearFacetParams, facetParam, facetsBeside, pickedFacetCount, toggleFacetParam } from "./AssetFacetSidebar";
import { criticalityShortLabel, orderFacetValues } from "./inventory";

const identity = (text: string) => text;

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

describe("asset facets at the window's width (#1501 UX r4 B1)", () => {
  test("draws the facets beside the table at 1728 and behind the Filters button at 1080, so the table never scrolls sideways", () => {
    expect(facetsBeside(1728)).toBe(true);
    expect(facetsBeside(1536)).toBe(true);
    expect(facetsBeside(1440)).toBe(false);
    expect(facetsBeside(1080)).toBe(false);
  });

  test("draws the Filters button at the one toolbar control height of Search, Columns, page size and Export (UX r5 B1)", () => {
    // Source guard (vitest runs from frontend/): the button takes the DataTable toolbar height constant, never a size of its own.
    const source = readFileSync(join(process.cwd(), "src/pages/xtm-one/ctem/AssetFacetSidebar.tsx"), "utf8");
    const button = source.slice(source.indexOf("export function AssetFacetsButton"));
    expect(button).toMatch(/<Button[^>]*\{cn\(TOOLBAR_CONTROL_HEIGHT,/s);
  });

  test("counts every picked value for the Filters badge", () => {
    expect(pickedFacetCount({ category: ["host", "ai_target"], subcategory: ["server"], criticality: [], internet_facing: ["yes"], source: [] })).toBe(4);
    expect(pickedFacetCount({ category: [], subcategory: [], criticality: [], internet_facing: [], source: [] })).toBe(0);
  });
});

describe("asset facet value order (#1501 UX r1 B3)", () => {
  test("lists criticality on its scale, most severe first, never by key", () => {
    const values = ["high", "low", "medium", "unknown", "very_high"].map((value) => ({ value, count: 1 }));
    expect(orderFacetValues("criticality", values, identity).map((v) => v.value)).toEqual(["very_high", "high", "medium", "low", "unknown"]);
  });

  test("lists internet facing as Yes, No, Unknown, never by key (UX r2 B1)", () => {
    const values = ["no", "unknown", "yes"].map((value) => ({ value, count: 1 }));
    expect(orderFacetValues("internet_facing", values, identity).map((v) => v.value)).toEqual(["yes", "no", "unknown"]);
  });

  test("sorts categories by their translated label and keeps the unclassified one last", () => {
    const labels: Record<string, string> = { host: "Host", generic_asset: "Other asset (to classify)", ai_target: "AI system", web_application: "Web application" };
    const values = ["generic_asset", "web_application", "host", "ai_target"].map((value) => ({ value, count: 1 }));
    expect(orderFacetValues("category", values, (v) => labels[v]).map((v) => v.value)).toEqual(["ai_target", "host", "web_application", "generic_asset"]);
  });

  test("keeps the order of a facet with no scale", () => {
    const values = ["openaev", "intune"].map((value) => ({ value, count: 1 }));
    expect(orderFacetValues("source", values, identity).map((v) => v.value)).toEqual(["openaev", "intune"]);
  });

  test("says Unknown under the Criticality heading, so the cell keeps one line (B2)", () => {
    expect(criticalityShortLabel(identity, "unknown")).toBe("Unknown");
    expect(criticalityShortLabel(identity, "very_high")).toBe("Very high (criticality)");
  });
});
