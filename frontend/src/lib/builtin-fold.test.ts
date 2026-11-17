// The built-in fold of the Agents and Fleets lists (#11831).

import { afterEach, describe, expect, it } from "vitest";

import {
  BUILTIN_PROPERTY_KEY,
  builtinLanding,
  builtinStorageKey,
  companySplit,
  companySplitKeeping,
  defaultBuiltinChoice,
  foldBuiltins,
  mergeBuiltinSelection,
  readBuiltinParam,
  readStoredBuiltin,
  splitBuiltinSelection,
  storeBuiltin,
  writeBuiltinParam,
} from "./builtin-fold";

type Row = { id: string; user_id: string | null; is_company_managed: boolean; is_builtin: boolean };
const row = (id: string, over: Partial<Row> = {}): Row => ({ id, user_id: null, is_company_managed: true, is_builtin: false, ...over });
const isBuiltin = (r: Row) => r.is_builtin;

describe("builtinLanding (no explicit segment): Mine, Company custom, Company built-ins, Shared, All", () => {
  it("1. Mine 40 / custom 1: Mine", () => {
    expect(builtinLanding({ mine: 40, company: 71, shared: 139, all: 250 }, { custom: 1, builtin: 70 })).toBe("mine");
    expect(builtinLanding({ mine: 3, company: 10, shared: 0, all: 13 }, { custom: 0, builtin: 10 })).toBe("mine");
  });
  it("2. Mine 0 / custom 1 / built-ins 70: Company, built-ins hidden", () => {
    expect(builtinLanding({ mine: 0, company: 71, shared: 139, all: 210 }, { custom: 1, builtin: 70 })).toBe("company");
    expect(defaultBuiltinChoice(1)).toBe("hide");
    expect(builtinLanding({ mine: 0, company: 80, shared: 0, all: 80 }, { custom: 10, builtin: 70 })).toBe("company");
  });
  it("3. Mine 0 / custom 0 / built-ins 70 / Shared 139: Company, built-ins shown, never Shared", () => {
    expect(builtinLanding({ mine: 0, company: 70, shared: 139, all: 209 }, { custom: 0, builtin: 70 })).toBe("company");
    // Even when the Company count handed in is the folded one (0), the split decides.
    expect(builtinLanding({ mine: 0, company: 0, shared: 139, all: 209 }, { custom: 0, builtin: 70 })).toBe("company");
    expect(defaultBuiltinChoice(0)).toBe("show");
  });
  it("4. nothing in Mine or Company: Shared, then All", () => {
    expect(builtinLanding({ mine: 0, company: 0, shared: 2, all: 2 }, { custom: 0, builtin: 0 })).toBe("shared");
    expect(builtinLanding({ mine: 0, company: 0, shared: 0, all: 0 }, { custom: 0, builtin: 0 })).toBe("all");
  });
});

describe("companySplit", () => {
  it("counts the company rows by origin, never a personal row", () => {
    const rows = [row("a"), row("b", { is_builtin: true }), row("c", { is_builtin: true }), row("d", { is_company_managed: false, user_id: "u1" })];
    expect(companySplit(rows, "u1", isBuiltin)).toEqual({ custom: 1, builtin: 2 });
  });
});

describe("foldBuiltins", () => {
  const rows = [row("a"), row("b", { is_builtin: true }), row("c", { is_builtin: true })];
  it("hides the built-ins of Company only", () => {
    expect(foldBuiltins(rows, "company", "hide", isBuiltin, false)).toEqual({ shown: [rows[0]], hidden: 2, revealedBySearch: false });
    expect(foldBuiltins(rows, "all", "hide", isBuiltin, false).shown).toHaveLength(3);
    expect(foldBuiltins(rows, "mine", "hide", isBuiltin, false).shown).toHaveLength(3);
  });
  it("keeps everything when shown", () => {
    expect(foldBuiltins(rows, "company", "show", isBuiltin, false)).toEqual({ shown: rows, hidden: 0, revealedBySearch: false });
  });
  it("shows the built-ins a search matches alone, never an empty list", () => {
    const builtins = rows.slice(1);
    expect(foldBuiltins(builtins, "company", "hide", isBuiltin, true)).toEqual({ shown: builtins, hidden: 0, revealedBySearch: true });
    // Without a search an empty custom set stays empty: the footer row offers Show.
    expect(foldBuiltins(builtins, "company", "hide", isBuiltin, false).shown).toHaveLength(0);
  });
});

describe("URL and storage", () => {
  afterEach(() => localStorage.clear());
  it("round-trips ?builtin=", () => {
    const params = writeBuiltinParam(new URLSearchParams("vis=company"), "show");
    expect(params.toString()).toBe("vis=company&builtin=show");
    expect(readBuiltinParam(params)).toBe("show");
    expect(readBuiltinParam(new URLSearchParams("builtin=nope"))).toBeNull();
  });
  it("stores the choice per user and per list", () => {
    storeBuiltin("agents", "u1", "show");
    expect(localStorage.getItem(builtinStorageKey("agents", "u1"))).toBe("show");
    expect(readStoredBuiltin("agents", "u1")).toBe("show");
    expect(readStoredBuiltin("fleets", "u1")).toBeNull();
    expect(readStoredBuiltin("agents", "u2")).toBeNull();
  });
});

describe("the Properties option", () => {
  it("is checked while built-ins are shown and split back out of the has: keys", () => {
    expect(mergeBuiltinSelection(["members"], "show")).toEqual(["members", BUILTIN_PROPERTY_KEY]);
    expect(mergeBuiltinSelection(["members"], "hide")).toEqual(["members"]);
    expect(splitBuiltinSelection(["members", BUILTIN_PROPERTY_KEY])).toEqual({ properties: ["members"], choice: "show" });
    expect(splitBuiltinSelection(["members"])).toEqual({ properties: ["members"], choice: "hide" });
  });
});

// The Agentic Flow map lands on the same rule, through the same helpers (#11882).
describe("the Agentic Flow map landing (#11882)", () => {
  const land = (rows: Row[]) => {
    const counts = {
      mine: rows.filter((r) => r.user_id === "u1" && !r.is_company_managed).length,
      company: rows.filter((r) => r.is_company_managed).length,
      shared: 0,
      all: rows.length,
    };
    const split = companySplit(rows, "u1", isBuiltin);
    return { segment: builtinLanding(counts, split), choice: defaultBuiltinChoice(split.custom), split };
  };
  it("Mine empty, company agents of the organisation's own: Company, built-ins hidden", () => {
    const rows = [row("custom"), row("b1", { is_builtin: true }), row("b2", { is_builtin: true })];
    const landed = land(rows);
    expect(landed).toEqual({ segment: "company", choice: "hide", split: { custom: 1, builtin: 2 } });
    expect(foldBuiltins(rows, landed.segment, landed.choice, isBuiltin, false)).toEqual({ shown: [rows[0]], hidden: 2, revealedBySearch: false });
  });
  it("built-ins only: Company with built-ins shown", () => {
    const rows = [row("b1", { is_builtin: true }), row("b2", { is_builtin: true })];
    expect(land(rows)).toMatchObject({ segment: "company", choice: "show" });
  });
  it("Mine holds agents and no company agent of the organisation's own: Mine", () => {
    const rows = [row("own", { user_id: "u1", is_company_managed: false }), row("b1", { is_builtin: true })];
    expect(land(rows).segment).toBe("mine");
  });
  it("Mine holds agents and the organisation has company agents of its own: Mine first", () => {
    const rows = [row("own", { user_id: "u1", is_company_managed: false }), row("custom"), row("b1", { is_builtin: true })];
    expect(land(rows)).toMatchObject({ segment: "mine", choice: "hide" });
  });
  // UX round 1 of #11946, B1: the General Assistant's stand-in is a built-in
  // the map keeps drawn; it never turns a catalogue of built-ins into a custom one.
  it("Mine 0 / custom 0 / built-ins 69 with the stand-in / Shared 18: Company, built-ins shown (Graph and Schedule)", () => {
    const builtins = Array.from({ length: 69 }, (_, i) => row(`b${i}`, { is_builtin: true }));
    const shared = Array.from({ length: 18 }, (_, i) => row(`s${i}`, { is_company_managed: false, user_id: "u2" }));
    const rows = [...builtins, ...shared];
    const split = companySplitKeeping(rows, "u1", isBuiltin, new Set(["b0"]));
    expect(split).toEqual({ landing: { custom: 0, builtin: 69 }, fold: { custom: 1, builtin: 68 } });
    const counts = { mine: 0, company: 69, shared: 18, all: 87 };
    expect(builtinLanding(counts, split.landing)).toBe("company");
    expect(defaultBuiltinChoice(split.landing.custom)).toBe("show");
  });
  it("Mine 0 / custom 1 / built-ins with the stand-in: Company, built-ins hidden, the stand-in still drawn", () => {
    const rows = [row("custom"), row("ga", { is_builtin: true }), row("b1", { is_builtin: true })];
    const split = companySplitKeeping(rows, "u1", isBuiltin, new Set(["ga"]));
    expect(builtinLanding({ mine: 0, company: 3, shared: 0, all: 3 }, split.landing)).toBe("company");
    expect(defaultBuiltinChoice(split.landing.custom)).toBe("hide");
    const kept = (r: Row) => r.is_builtin && r.id !== "ga";
    expect(foldBuiltins(rows, "company", "hide", kept, false)).toEqual({ shown: [rows[0], rows[1]], hidden: 1, revealedBySearch: false });
  });
  it("Mine 40 / custom 1 with the stand-in: Mine", () => {
    const own = Array.from({ length: 40 }, (_, i) => row(`m${i}`, { user_id: "u1", is_company_managed: false }));
    const rows = [...own, row("custom"), row("ga", { is_builtin: true })];
    const split = companySplitKeeping(rows, "u1", isBuiltin, new Set(["ga"]));
    expect(builtinLanding({ mine: 40, company: 2, shared: 0, all: 42 }, split.landing)).toBe("mine");
  });
});
