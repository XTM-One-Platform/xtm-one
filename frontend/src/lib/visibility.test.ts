// The one visibility convention of every list page (dev:visibility-filters-consistency).

import { describe, expect, it } from "vitest";

import {
  DEFAULT_VISIBILITY,
  VISIBILITY_SEGMENTS,
  countSegments,
  inSegment,
  landingVisibility,
  namedVisibility,
  readVisibility,
  resolveLanding,
  segmentOf,
  toVisibility,
  visibilityUpdate,
  writeVisibility,
} from "./visibility";

const params = (query: string) => new URLSearchParams(query);

describe("the visibility convention", () => {
  it("has four segments in one order, Mine by default", () => {
    expect(VISIBILITY_SEGMENTS).toEqual(["mine", "company", "shared", "all"]);
    expect(DEFAULT_VISIBILITY).toBe("mine");
  });

  it.each([
    // counts -> the segment a page lands on with no segment in its URL
    [{ mine: 2, company: 9, shared: 1, all: 12 }, "mine"],
    [{ mine: 0, company: 9, shared: 0, all: 9 }, "company"],
    [{ mine: 0, company: 9, shared: 3, all: 12 }, "company"],
    [{ mine: 0, company: 0, shared: 3, all: 3 }, "shared"],
    [{ mine: 0, company: 0, shared: 0, all: 4 }, "all"],
    [{ mine: 0, company: 0, shared: 0, all: 0 }, "all"],
    [{ mine: 0, all: 4 }, "all"],
    [{}, "all"],
  ] as const)("lands on the first segment holding an item: %o -> %s", (counts, expected) => {
    expect(landingVisibility(counts)).toBe(expected);
  });

  it("lands only on a segment the page offers", () => {
    expect(landingVisibility({ mine: 0, company: 5, all: 5 }, ["mine", "all"])).toBe("all");
    expect(landingVisibility({ mine: 0, company: 0, shared: 2 }, ["mine", "shared"])).toBe("shared");
    expect(landingVisibility({ mine: 0 }, ["mine", "shared"])).toBe("mine");
  });

  it.each([
    // named, counts, options -> the state of a server-filtered list
    ["mine", { mine: 0, company: 3, all: 3 }, {}, { visibility: "mine", pending: false, land: false }],
    ["shared", undefined, {}, { visibility: "shared", pending: false, land: false }],
    [null, undefined, {}, { visibility: "mine", pending: true, land: false }],
    [null, { mine: 0, company: 3, all: 3 }, {}, { visibility: "company", pending: false, land: true }],
    [null, { mine: 2, company: 3, all: 5 }, {}, { visibility: "mine", pending: false, land: true }],
    [null, { mine: 0, company: 3, all: 3 }, { offered: ["mine", "all"] }, { visibility: "all", pending: false, land: true }],
    [null, undefined, { enabled: false, fallback: "all" }, { visibility: "all", pending: false, land: false }],
    [null, { mine: 0, company: 3, all: 3 }, { enabled: false }, { visibility: "mine", pending: false, land: false }],
    // Counts that failed to load: the fallback at once, never a wait.
    [null, null, {}, { visibility: "mine", pending: false, land: true }],
    ["company", null, {}, { visibility: "company", pending: false, land: false }],
  ] as const)("resolves a server-filtered landing: %s %o %o", (named, counts, options, expected) => {
    expect(resolveLanding(named, counts, options)).toEqual(expected);
  });

  it("names a segment only when the URL does, so the landing rule applies otherwise", () => {
    expect(namedVisibility(params(""))).toBeNull();
    expect(namedVisibility(params("vis=nonsense"))).toBeNull();
    expect(namedVisibility(params("vis=mine"))).toBe("mine");
    expect(namedVisibility(params("ownership=shared"))).toBe("all");
    expect(namedVisibility(params("vis=company"), ["mine", "all"])).toBeNull();
  });

  it("reads a segment, the [DEPRECATED] values, and nothing else", () => {
    expect(toVisibility("Company")).toBe("company");
    expect(toVisibility("company_managed")).toBe("company");
    expect(toVisibility("own")).toBe("mine");
    expect(toVisibility("everyone")).toBe("all");
    expect(toVisibility("min")).toBeNull();
    expect(toVisibility("")).toBeNull();
    expect(toVisibility(null)).toBeNull();
  });

  it("lands on Mine when the URL names no segment", () => {
    expect(readVisibility(params(""))).toBe("mine");
    expect(readVisibility(params("vis=nonsense"))).toBe("mine");
  });

  it("reads ?vis= first, then the [DEPRECATED] aliases in order", () => {
    expect(readVisibility(params("vis=all&visibility=mine"))).toBe("all");
    expect(readVisibility(params("visibility=shared"))).toBe("shared");
    expect(readVisibility(params("ownership=mine"))).toBe("mine");
    expect(readVisibility(params("scope=all"))).toBe("all");
  });

  it("opens an old ?ownership=shared|company_managed link (shared + company) on All, hiding none of its rows", () => {
    expect(readVisibility(params("ownership=shared"))).toBe("all");
    expect(readVisibility(params("ownership=company_managed"))).toBe("all");
    // The other aliases keep their one-segment meaning.
    expect(readVisibility(params("visibility=company_managed"))).toBe("company");
    expect(readVisibility(params("visibility=shared"))).toBe("shared");
  });

  it("opens an old ?owner=shared link (Shared with me: shared + company) on All on every page", () => {
    expect(readVisibility(params("owner=shared"))).toBe("all");
    expect(readVisibility(params("owner=SHARED"))).toBe("all");
    expect(readVisibility(params("owner=mine"))).toBe("mine");
    expect(readVisibility(params("owner=company"))).toBe("company");
    expect(readVisibility(params("owner=all"))).toBe("all");
    // ?vis= still wins over the alias.
    expect(readVisibility(params("vis=mine&owner=shared"))).toBe("mine");
  });

  it("falls back when the page does not offer the segment asked for", () => {
    expect(readVisibility(params("vis=company"), ["mine", "shared", "all"])).toBe("mine");
  });

  it("writes ?vis= and drops every [DEPRECATED] alias", () => {
    const next = writeVisibility(params("visibility=shared&ownership=all&owner=shared&scope=mine&q=x"), "company");
    expect(next.toString()).toBe("q=x&vis=company");
    expect(visibilityUpdate("mine")).toEqual({ vis: "mine", visibility: null, ownership: null, owner: null, scope: null });
  });

  it("puts every item in one segment, a stranger's private row in All only", () => {
    const own = { user_id: "u1", is_company_managed: false };
    const company = { user_id: "u1", is_company_managed: true };
    const shared = { user_id: "u2", is_company_managed: false, shared_group_ids: ["g1"] };
    const stranger = { user_id: "u3", is_company_managed: false, shared_group_ids: [] };
    const served = { user_id: "u2", is_company_managed: false };
    const direct = { user_id: "u4", is_company_managed: false, shared_group_ids: [], shared_user_ids: ["u1"] };
    expect(segmentOf(own, "u1")).toBe("mine");
    expect(segmentOf(company, "u1")).toBe("company");
    expect(segmentOf(shared, "u1")).toBe("shared");
    expect(segmentOf(stranger, "u1")).toBeNull();
    expect(segmentOf(served, "u1")).toBe("shared");
    expect(segmentOf(direct, "u1")).toBe("shared");
    expect(inSegment(stranger, "all", "u1")).toBe(true);
    expect(countSegments([own, company, shared, stranger, served, direct], "u1")).toEqual({
      mine: 1,
      company: 1,
      shared: 3,
      all: 6,
    });
  });
});
