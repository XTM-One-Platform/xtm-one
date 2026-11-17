/**
 * The view state of the agent graph pages - `yarn test:unit` (vitest).
 */
import { describe, expect, it } from "vitest";

import { DEFAULT_GRAPH_FILTERS } from "./agent-graph-filters";
import { builtinLanding, defaultBuiltinChoice } from "./builtin-fold";
import type { Visibility } from "./visibility";
import {
  FLEET_VIEW_DEFAULTS,
  MAP_VIEW_DEFAULTS,
  graphViewStorageKey,
  mapSegmentNamed,
  parseForces,
  parseStoredView,
  readViewFromUrl,
  resolveGraphView,
  serializeView,
  urlHasView,
  writeViewToUrl,
  type GraphViewState,
} from "./agent-graph-view-state";

const params = (query: string) => new URLSearchParams(query);

const CHOSEN: GraphViewState = {
  layout: "hierarchy-lr",
  layoutChosen: true,
  visibility: "company",
  filters: { kinds: ["builtin", "personal"], enabled: "enabled", withOpenWorkItems: true, withRunsInFlight: false },
  forces: { charge: 1.5, linkDistance: 200, collide: 140 },
  fleetsLayer: false,
};

describe("defaults", () => {
  it("lands on Mine and Force on the map with no URL and no preference", () => {
    expect(resolveGraphView(params(""), null, MAP_VIEW_DEFAULTS)).toEqual(MAP_VIEW_DEFAULTS);
    expect(MAP_VIEW_DEFAULTS.layout).toBe("force");
    expect(MAP_VIEW_DEFAULTS.visibility).toBe("mine");
  });

  it("lands on Force on a fleet, with no segment", () => {
    const view = resolveGraphView(params(""), null, FLEET_VIEW_DEFAULTS);
    expect(view.layout).toBe("force");
    expect(view.visibility).toBeUndefined();
  });
});

describe("URL round trip", () => {
  it("reads back every choice it wrote", () => {
    const url = writeViewToUrl(params(""), CHOSEN, MAP_VIEW_DEFAULTS);
    expect(readViewFromUrl(url, MAP_VIEW_DEFAULTS)).toEqual(CHOSEN);
    // Kinds come back in their canonical order, whatever order they were picked in.
    const picked = writeViewToUrl(params(""), { ...CHOSEN, filters: { ...CHOSEN.filters, kinds: ["personal", "builtin"] } }, MAP_VIEW_DEFAULTS);
    expect(picked.get("kinds")).toBe("builtin,personal");
  });

  it("writes only what differs from the defaults, and always the segment", () => {
    const url = writeViewToUrl(params(""), MAP_VIEW_DEFAULTS, MAP_VIEW_DEFAULTS);
    expect(url.toString()).toBe("vis=mine");
    const lr = writeViewToUrl(params(""), { ...MAP_VIEW_DEFAULTS, layout: "hierarchy-lr" }, MAP_VIEW_DEFAULTS);
    expect(lr.get("layout")).toBe("hierarchy-lr");
    expect(lr.has("kinds")).toBe(false);
  });

  it("keeps the keys that are not the view's and drops the deprecated aliases", () => {
    const url = writeViewToUrl(params("view=schedule&agent=a1&ownership=own"), MAP_VIEW_DEFAULTS, MAP_VIEW_DEFAULTS);
    expect(url.get("view")).toBe("schedule");
    expect(url.get("agent")).toBe("a1");
    expect(url.has("ownership")).toBe(false);
  });

  it("removes a key set back to its default", () => {
    const url = writeViewToUrl(params("layout=radial&items=1"), MAP_VIEW_DEFAULTS, MAP_VIEW_DEFAULTS);
    expect(url.has("layout")).toBe(false);
    expect(url.has("items")).toBe(false);
  });

  it("takes the default for a missing or invalid key", () => {
    const view = readViewFromUrl(params("layout=spiral&enabled=maybe&forces=9,9,9&fleets=x&vis=all"), MAP_VIEW_DEFAULTS);
    expect(view.layout).toBe("force");
    expect(view.filters.enabled).toBe("all");
    expect(view.forces).toBeNull();
    expect(view.fleetsLayer).toBe(true);
    expect(view.visibility).toBe("all");
  });

  it("reads an empty kinds list as every kind", () => {
    expect(readViewFromUrl(params("kinds="), MAP_VIEW_DEFAULTS).filters.kinds).toEqual([]);
    expect(readViewFromUrl(params("kinds=company,nope"), MAP_VIEW_DEFAULTS).filters.kinds).toEqual(["company"]);
  });
});

describe("preference", () => {
  it("restores the stored view when the URL names none, except the segment", () => {
    // Layout, filters, forces and the Fleets layer come back; the segment is the map's default until it lands.
    expect(resolveGraphView(params("view=graph"), serializeView(CHOSEN), MAP_VIEW_DEFAULTS)).toEqual({ ...CHOSEN, visibility: "mine" });
  });

  // #12071: a segment picked on an earlier visit never outlives the counts it was picked on.
  it("never stores nor restores the segment, so the map lands from its counts on every visit", () => {
    expect(JSON.parse(serializeView({ ...MAP_VIEW_DEFAULTS, visibility: "company" }))).not.toHaveProperty("visibility");
    expect(JSON.parse(serializeView(MAP_VIEW_DEFAULTS))).not.toHaveProperty("visibilityChosen");
    // Stored by an older build: a pick of Mine (or Shared) is ignored, its flag dropped.
    const stuck = parseStoredView('{"visibility":"mine","visibilityChosen":true,"layout":"radial","layoutChosen":true}', MAP_VIEW_DEFAULTS);
    expect(stuck).toEqual({ ...MAP_VIEW_DEFAULTS, layout: "radial", layoutChosen: true });
    expect(stuck).not.toHaveProperty("visibilityChosen");
    expect(parseStoredView('{"visibility":"shared"}', MAP_VIEW_DEFAULTS)?.visibility).toBe("mine");
    // A fleet has no segment.
    expect(parseStoredView('{"visibility":"shared"}', FLEET_VIEW_DEFAULTS)).not.toHaveProperty("visibility");
  });

  it("names the map's segment from ?vis=, a deprecated alias or a focus link only", () => {
    expect(mapSegmentNamed(params("vis=mine"))).toBe(true);
    expect(mapSegmentNamed(params("ownership=own"))).toBe(true);
    expect(mapSegmentNamed(params("focus=a1"))).toBe(true);
    expect(mapSegmentNamed(params(""))).toBe(false);
    expect(mapSegmentNamed(params("layout=radial&vis=nope"))).toBe(false);
  });

  // The map's segment as MapTab decides it: the URL when it names one, else `builtinLanding`.
  describe("the map landing with a stored pick of Mine (#12071)", () => {
    const STUCK = '{"visibility":"mine","visibilityChosen":true}';
    const land = (query: string, counts: Partial<Record<Visibility, number>>, company: { custom: number; builtin: number }) => {
      const url = params(query);
      const view = resolveGraphView(url, STUCK, MAP_VIEW_DEFAULTS);
      return mapSegmentNamed(url) ? view.visibility : builtinLanding(counts, company);
    };
    it("Mine 0 / Company 12 / All 79: Company", () => {
      expect(land("", { mine: 0, company: 12, shared: 67, all: 79 }, { custom: 12, builtin: 0 })).toBe("company");
    });
    it("Mine 40: Mine", () => {
      expect(land("", { mine: 40, company: 12, shared: 27, all: 79 }, { custom: 12, builtin: 0 })).toBe("mine");
    });
    it("an explicit ?vis=mine wins, an empty Mine included", () => {
      expect(land("vis=mine", { mine: 0, company: 12, shared: 67, all: 79 }, { custom: 12, builtin: 0 })).toBe("mine");
    });
    it("Mine 0 / custom 0 / built-ins 70 / Shared 139: Company, built-ins shown", () => {
      const company = { custom: 0, builtin: 70 };
      expect(land("", { mine: 0, company: 70, shared: 139, all: 209 }, company)).toBe("company");
      expect(defaultBuiltinChoice(company.custom)).toBe("show");
    });
  });

  it("prefers the URL as soon as it names one view key, the rest at the defaults", () => {
    const view = resolveGraphView(params("layout=radial"), serializeView(CHOSEN), MAP_VIEW_DEFAULTS);
    expect(view).toEqual({ ...MAP_VIEW_DEFAULTS, layout: "radial", layoutChosen: true });
  });

  it("counts a deprecated segment alias as a view key on the map", () => {
    expect(urlHasView(params("ownership=own"), MAP_VIEW_DEFAULTS)).toBe(true);
  });

  it("ignores ?vis= on a fleet, whose other tabs own it", () => {
    expect(urlHasView(params("vis=all"), FLEET_VIEW_DEFAULTS)).toBe(false);
    const stored = serializeView({ ...FLEET_VIEW_DEFAULTS, layout: "radial", layoutChosen: true });
    expect(resolveGraphView(params("vis=all"), stored, FLEET_VIEW_DEFAULTS).layout).toBe("radial");
  });

  it("falls back to the defaults for an unreadable or foreign value", () => {
    expect(parseStoredView("{not json", MAP_VIEW_DEFAULTS)).toBeNull();
    expect(parseStoredView("[1,2]", MAP_VIEW_DEFAULTS)).toBeNull();
    expect(resolveGraphView(params(""), "null", MAP_VIEW_DEFAULTS)).toEqual(MAP_VIEW_DEFAULTS);
    const partial = parseStoredView('{"layout":"radial","visibility":"nope","filters":{"kinds":["x","company"]}}', MAP_VIEW_DEFAULTS);
    expect(partial).toEqual({ ...MAP_VIEW_DEFAULTS, layout: "radial", layoutChosen: true, filters: { ...DEFAULT_GRAPH_FILTERS, kinds: ["company"] } });
  });

  it("keeps a picked layout and drops a layout that was only the default when written", () => {
    // Picked on the toolbar: kept on the next visit.
    const picked = serializeView({ ...FLEET_VIEW_DEFAULTS, layout: "hierarchy-lr", layoutChosen: true });
    expect(resolveGraphView(params(""), picked, FLEET_VIEW_DEFAULTS).layout).toBe("hierarchy-lr");
    // Written along with another change, never picked: the page's default wins.
    const unpicked = serializeView({ ...FLEET_VIEW_DEFAULTS, layout: "radial", fleetsLayer: false });
    const view = resolveGraphView(params(""), unpicked, FLEET_VIEW_DEFAULTS);
    expect(view.layout).toBe("force");
    expect(view.fleetsLayer).toBe(false);
  });

  it("migrates a fleet preference stored before the flag: Left to right was the default, any other layout a choice", () => {
    expect(resolveGraphView(params(""), '{"layout":"hierarchy-lr","fleetsLayer":false}', FLEET_VIEW_DEFAULTS).layout).toBe("force");
    expect(resolveGraphView(params(""), '{"layout":"radial"}', FLEET_VIEW_DEFAULTS).layout).toBe("radial");
    expect(resolveGraphView(params(""), '{"layout":"force"}', FLEET_VIEW_DEFAULTS).layoutChosen).toBe(true);
    // On the map Force was already the default: a stored Force is the default.
    expect(parseStoredView('{"layout":"force"}', MAP_VIEW_DEFAULTS)?.layoutChosen).toBeUndefined();
  });

  it("keys the preference per user and per page", () => {
    expect(graphViewStorageKey("u1", "map")).not.toBe(graphViewStorageKey("u2", "map"));
    expect(graphViewStorageKey("u1", "fleet:f1")).not.toBe(graphViewStorageKey("u1", "fleet:f2"));
  });
});

describe("parseForces", () => {
  it("accepts three numbers inside the toolbar's bounds only", () => {
    expect(parseForces("1,160,120")).toEqual({ charge: 1, linkDistance: 160, collide: 120 });
    expect(parseForces("1,160")).toBeNull();
    expect(parseForces("1,,120")).toBeNull();
    expect(parseForces("1,5000,120")).toBeNull();
    expect(parseForces(null)).toBeNull();
  });
});

describe("no graph mode: Data flow is a fleet tab of its own (#9980)", () => {
  it("drops the mode a preference stored for the former Agents | Data flow switch (#9829)", () => {
    const stored = JSON.stringify({ ...FLEET_VIEW_DEFAULTS, layout: "radial", layoutChosen: true, mode: "flow" });
    const view = resolveGraphView(params(""), stored, FLEET_VIEW_DEFAULTS);
    expect(view).not.toHaveProperty("mode");
    expect(view.layout).toBe("radial");
    expect(serializeView(view)).not.toContain("mode");
  });

  it("never reads nor writes mode in the URL", () => {
    expect(urlHasView(params("mode=flow"), FLEET_VIEW_DEFAULTS)).toBe(false);
    expect(urlHasView(params("mode=flow"), MAP_VIEW_DEFAULTS)).toBe(false);
    expect(readViewFromUrl(params("mode=flow"), FLEET_VIEW_DEFAULTS)).not.toHaveProperty("mode");
    // Another key of the page's query is kept as it is.
    expect(writeViewToUrl(params("mode=flow"), FLEET_VIEW_DEFAULTS, FLEET_VIEW_DEFAULTS).get("mode")).toBe("flow");
  });
});
