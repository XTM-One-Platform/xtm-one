import { describe, expect, it } from "vitest";

import {
  effectiveFleetParam,
  FLEET_DEFAULT_VIEW,
  FLEET_TABS,
  fleetAccess,
  fleetParamPending,
  fleetViewPath,
  isKnownFleetTab,
  legacyFleetLocation,
  offersFleetPicker,
  readFleetOpsList,
  readFleetTab,
  readLegacyFleetView,
} from "./fleet-operations";

const params = (query: string) => new URLSearchParams(query);

describe("readFleetTab", () => {
  it("opens a fleet on About, the page with no tab, and reads every tab segment (#10203)", () => {
    expect(readFleetTab(undefined)).toBe("about");
    expect(FLEET_DEFAULT_VIEW).toBe(FLEET_TABS[0]);
    expect(readFleetTab("graph")).toBe("graph");
    expect(readFleetTab("data-flow")).toBe("flow");
    expect(readFleetTab("members")).toBe("members");
    expect(readFleetTab("approval")).toBe("approval");
    expect(readFleetTab("schedule")).toBe("schedule");
    expect(readFleetTab("operations")).toBe("operations");
    expect(readFleetTab("roi")).toBe("roi");
    expect(readFleetTab("nope")).toBe("about");
    // Sam's order and cap (#10176): eight tabs, About first, Approval after Members.
    expect(FLEET_TABS).toEqual(["about", "graph", "flow", "members", "approval", "schedule", "operations", "roi"]);
    expect(FLEET_TABS.length).toBeLessThanOrEqual(8);
  });

  it("knows which segments name a tab, so an unknown one is replaced by About", () => {
    expect(isKnownFleetTab(undefined)).toBe(true);
    for (const segment of ["graph", "data-flow", "members", "approval", "schedule", "operations", "roi"]) {
      expect(isKnownFleetTab(segment)).toBe(true);
    }
    expect(isKnownFleetTab("flow")).toBe(false);
    expect(isKnownFleetTab("about")).toBe(false);
    expect(isKnownFleetTab("nope")).toBe(false);
  });
});

describe("fleetViewPath", () => {
  it("writes one path per tab, About with none and Operations on a list", () => {
    expect(fleetViewPath("f", "about")).toBe("/fleets/f");
    expect(fleetViewPath("f", "graph")).toBe("/fleets/f/graph");
    expect(fleetViewPath("f", "flow")).toBe("/fleets/f/data-flow");
    expect(fleetViewPath("f", "members")).toBe("/fleets/f/members");
    expect(fleetViewPath("f", "approval")).toBe("/fleets/f/approval");
    expect(fleetViewPath("f", "schedule")).toBe("/fleets/f/schedule");
    expect(fleetViewPath("f", "roi")).toBe("/fleets/f/roi");
    expect(fleetViewPath("f", "operations")).toBe("/fleets/f/operations/runs");
    expect(fleetViewPath("f", "operations", "work-items")).toBe("/fleets/f/operations/work-items");
  });

  it("round-trips: the segment a path writes reads back as its tab", () => {
    for (const view of FLEET_TABS) {
      // `/fleets/f/<tab>` (#9301 N2c): the tab is the third segment.
      const segment = fleetViewPath("f", view).split("/")[3];
      expect(readFleetTab(segment)).toBe(view);
    }
  });
});

describe("legacy links (?view=, ?ops=, ?mode=)", () => {
  it("reads the view an old query named, null when it names none", () => {
    expect(readLegacyFleetView(params(""))).toBeNull();
    expect(readLegacyFleetView(params("window=7d"))).toBeNull();
    expect(readLegacyFleetView(params("view=graph"))).toBe("graph");
    expect(readLegacyFleetView(params("view=about"))).toBe("about");
    expect(readLegacyFleetView(params("view=nope"))).toBe("about");
    expect(readLegacyFleetView(params("view=flow"))).toBe("flow");
    // The Graph tab's former "Agents | Data flow" switch (#9829).
    expect(readLegacyFleetView(params("view=graph&mode=flow"))).toBe("flow");
    expect(readLegacyFleetView(params("mode=flow"))).toBe("flow");
    expect(readLegacyFleetView(params("view=about&mode=flow"))).toBe("about");
    expect(readLegacyFleetView(params("ops=runs&mode=flow"))).toBe("operations");
  });

  it("redirects each old link to its path, the rest of the query kept", () => {
    expect(legacyFleetLocation("f", params(""))).toBeNull();
    expect(legacyFleetLocation("f", params("window=7d"))).toBeNull();
    expect(legacyFleetLocation("f", params("view=graph"))).toBe("/fleets/f/graph");
    expect(legacyFleetLocation("f", params("view=about"))).toBe("/fleets/f");
    expect(legacyFleetLocation("f", params("view=nope"))).toBe("/fleets/f");
    expect(legacyFleetLocation("f", params("view=roi&window=7d"))).toBe("/fleets/f/roi?window=7d");
    expect(legacyFleetLocation("f", params("view=graph&mode=flow"))).toBe("/fleets/f/data-flow");
    expect(legacyFleetLocation("f", params("mode=flow&layout=radial"))).toBe("/fleets/f/data-flow?layout=radial");
    expect(legacyFleetLocation("f", params("ops=runs&review=unreviewed"))).toBe("/fleets/f/operations/runs?review=unreviewed");
    expect(legacyFleetLocation("f", params("ops=executions"))).toBe("/fleets/f/operations/runs");
    // The work items' Stale segment owned `?view=` inside Operations: it stays a query of the list.
    expect(legacyFleetLocation("f", params("ops=work-items&view=stale"))).toBe("/fleets/f/operations/work-items?view=stale");
  });
});

describe("readFleetOpsList", () => {
  it("reads the five lists and falls back to the runs", () => {
    expect(readFleetOpsList(null)).toBe("runs");
    for (const list of ["runs", "background-tasks", "work-items", "state", "worksets"] as const) {
      expect(readFleetOpsList(list)).toBe(list);
    }
    expect(readFleetOpsList("executions")).toBe("runs");
  });
});

describe("offersFleetPicker", () => {
  it("shows the picker only on a list locked on neither a fleet nor a workspace", () => {
    expect(offersFleetPicker({})).toBe(true);
    expect(offersFleetPicker({ fleet: "f1" })).toBe(false);
    // The workspace page's Operations tab.
    expect(offersFleetPicker({ workspace: "w1" })).toBe(false);
    expect(offersFleetPicker({ fleet: "f1", workspace: "w1" })).toBe(false);
  });
});

describe("fleetAccess", () => {
  // `GET /fleets` lists the reader's own, shared and company-managed fleets.
  const mine = { id: "f-mine", user_id: "u1", is_company_managed: false };
  const shared = { id: "f-shared", user_id: "u2", is_company_managed: false, shared_user_ids: ["u1"] };
  const company = { id: "f-company", user_id: null, is_company_managed: true };

  it("hides the filter for a reader with no accessible fleet", () => {
    expect(fleetAccess([], false)).toBe("none");
  });

  it("shows it for each kind of fleet alone", () => {
    for (const fleet of [mine, shared, company]) {
      expect(fleetAccess([fleet], false)).toBe("some");
    }
  });

  it("is unknown while the list loads, none when it is refused", () => {
    expect(fleetAccess(undefined, false)).toBe("unknown");
    expect(fleetAccess(undefined, true)).toBe("none");
  });
});

describe("effectiveFleetParam", () => {
  it("ignores ?fleet= for a reader who can access no fleet", () => {
    expect(effectiveFleetParam("f1", "none")).toBeUndefined();
    expect(effectiveFleetParam("f1", "some")).toBe("f1");
    expect(effectiveFleetParam("f1", "unknown")).toBe("f1");
    expect(effectiveFleetParam(undefined, "some")).toBeUndefined();
  });
});

describe("fleetParamPending", () => {
  it("waits only while a URL fleet meets an unknown access", () => {
    expect(fleetParamPending("f1", "unknown")).toBe(true);
    expect(fleetParamPending("f1", "some")).toBe(false);
    expect(fleetParamPending("f1", "none")).toBe(false);
    expect(fleetParamPending(undefined, "unknown")).toBe(false);
  });
});
