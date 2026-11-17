import { describe, expect, it } from "vitest";

import type { FleetMember } from "@/hooks/api/useFleetApi";
import type { Organization } from "@/hooks/api/useOrganizationsApi";

import { CONTEXT_INCLUSION_LABELS, CONTEXT_INCLUSION_SHORT_LABELS } from "./context-inclusion";
import {
  defaultImpact,
  firstLine,
  isConfigurable,
  isVisible,
  membersTableRows,
  readMembersTableState,
  RESET_SETTING_LABELS,
  resetCalls,
  sameScope,
  scopeFromPicks,
  scopeTags,
  settleAll,
  takesSetting,
  toggleSelection,
  trackedOrganizations,
  undoBulkCalls,
  visibleMembersRows,
  writeMembersTableState,
  type MembersTableState,
} from "./members-view";

function org(id: string, name: string, role: Organization["role"] = "tracked", archived = false): Organization {
  return {
    id,
    name,
    role,
    relationship: role === "primary" ? null : "provider",
    criticality: "medium",
    opencti_organization_id: null,
    monitoring: "off",
    created_at: "2026-11-06T00:00:00Z",
    archived_at: archived ? "2026-11-06T00:00:00Z" : null,
  };
}

const ORGS = [org("p", "Acme", "primary"), org("t1", "Supplier"), org("t2", "Partner"), org("t3", "Old", "tracked", true)];

function member(overrides: Partial<FleetMember> = {}): FleetMember {
  return {
    id: "m",
    fleet_id: "f",
    role: "connected",
    agent_id: "a",
    name: "Agent",
    target_fleet_id: null,
    parent_member_id: null,
    is_orchestrator: false,
    is_exposed: false,
    exposed_explicitly: false,
    role_label: "",
    collaboration_instruction: "",
    context_inclusion: "none",
    overridden: [],
    created_at: "2026-11-06T00:00:00Z",
    ...overrides,
  };
}

describe("membersTableRows", () => {
  it("lists the orchestrator, the home members, then the connected rows", () => {
    const rows = membersTableRows([
      member({ id: "c", name: "Zed" }),
      member({ id: "h2", role: "member", name: "Beta" }),
      member({ id: "o", role: "member", name: "Zulu", is_orchestrator: true }),
      member({ id: "h1", role: "member", name: "Alpha" }),
    ]);
    expect(rows.map((r) => r.id)).toEqual(["o", "h1", "h2", "c"]);
  });
});

describe("members table state in the URL", () => {
  it("reads the defaults when the URL carries nothing or an unknown value", () => {
    expect(readMembersTableState(new URLSearchParams("mrole=boss&msort=x&mdir=up"))).toEqual({
      query: "",
      role: "all",
      state: "all",
      sort: null,
      direction: "asc",
    });
  });

  it("round-trips a state and keeps the other parameters", () => {
    const state: MembersTableState = { query: "ag", role: "connected", state: "overridden", sort: "organizations", direction: "desc" };
    const params = writeMembersTableState(new URLSearchParams("view=members"), state);
    expect(params.get("view")).toBe("members");
    expect(readMembersTableState(params)).toEqual(state);
  });

  it("leaves a default value out of the URL", () => {
    const params = writeMembersTableState(new URLSearchParams("view=members&mq=x&mdir=desc"), {
      query: "",
      role: "all",
      state: "all",
      sort: null,
      direction: "desc",
    });
    expect(params.toString()).toBe("view=members");
  });
});

describe("visibleMembersRows", () => {
  const rows = membersTableRows([
    member({ id: "o", role: "member", name: "Lead", is_orchestrator: true }),
    member({ id: "h", role: "member", name: "Analyst", overridden: ["organization_scope"] }),
    member({ id: "c1", name: "Beacon", overridden: ["context_inclusion"] }),
    member({ id: "c2", name: "Atlas" }),
    member({ id: "x", name: "" }),
  ]);
  const base: MembersTableState = { query: "", role: "all", state: "all", sort: null, direction: "asc" };
  const ids = (state: Partial<MembersTableState>, scope = (m: FleetMember) => m.id) =>
    visibleMembersRows(rows, { ...base, ...state }, scope).map((m) => m.id);

  it("keeps the table order with no search, filter or sort", () => {
    expect(ids({})).toEqual(["o", "h", "x", "c2", "c1"]);
  });

  it("searches the member name, case-insensitive, never a hidden row", () => {
    expect(ids({ query: "A" })).toEqual(["o", "h", "c2", "c1"]);
    expect(ids({ query: "  beac " })).toEqual(["c1"]);
  });

  it("filters on the role and on the setting state", () => {
    expect(ids({ role: "orchestrator" })).toEqual(["o"]);
    expect(ids({ role: "member" })).toEqual(["h"]);
    expect(ids({ role: "connected" })).toEqual(["x", "c2", "c1"]);
    expect(ids({ state: "overridden" })).toEqual(["h", "c1"]);
    expect(ids({ state: "default" })).toEqual(["o", "x", "c2"]);
  });

  it("sorts on the member, the role and the organizations, both ways", () => {
    expect(ids({ sort: "member", role: "connected", query: "a" })).toEqual(["c2", "c1"]);
    expect(ids({ sort: "member", direction: "desc", query: "a" })).toEqual(["o", "c1", "c2", "h"]);
    expect(ids({ sort: "role", direction: "desc" })).toEqual(["x", "c2", "c1", "h", "o"]);
    expect(ids({ sort: "organizations" }, (m) => ({ o: "c", h: "a", c1: "b", c2: "b", x: "d" })[m.id] ?? "")).toEqual(["h", "c2", "c1", "o", "x"]);
  });
});

describe("isConfigurable", () => {
  it("is a connected row the reader can see", () => {
    expect(isConfigurable(member())).toBe(true);
    expect(isConfigurable(member({ role: "member" }))).toBe(false);
    expect(isConfigurable(member({ name: "" }))).toBe(false);
    expect(isConfigurable(member({ name: "", agent_id: null, target_fleet_id: "f2" }))).toBe(true);
  });
});

describe("toggleSelection", () => {
  const ids = ["a", "b", "c", "d", "e"];

  it("toggles one row on a plain click", () => {
    expect([...toggleSelection(new Set(), ids, "b", null, false)]).toEqual(["b"]);
    expect([...toggleSelection(new Set(["b"]), ids, "b", "b", false)]).toEqual([]);
  });

  it("selects the range from the anchor on Shift+click, either direction", () => {
    expect([...toggleSelection(new Set(["b"]), ids, "e", "b", true)].sort()).toEqual(["b", "c", "d", "e"]);
    expect([...toggleSelection(new Set(["d"]), ids, "a", "d", true)].sort()).toEqual(["a", "b", "c", "d"]);
  });

  it("clears the range when the clicked row was selected", () => {
    expect([...toggleSelection(new Set(ids), ids, "d", "b", true)].sort()).toEqual(["a", "e"]);
  });

  it("falls back to a single toggle without a known anchor", () => {
    expect([...toggleSelection(new Set(), ids, "c", "gone", true)]).toEqual(["c"]);
  });
});

describe("defaultImpact", () => {
  it("counts the rows that inherit a default and the ones that keep their override", () => {
    const rows = [
      member({ id: "1" }),
      member({ id: "2", overridden: ["context_inclusion"] }),
      member({ id: "3", overridden: ["collaboration_instruction"] }),
      member({ id: "h", role: "member" }),
    ];
    expect(defaultImpact(rows, "context_inclusion")).toEqual({ applies: 2, keep: 1 });
  });
});

describe("undoBulkCalls", () => {
  it("sends an inherited field back to null and an overridden one to its own value", () => {
    const calls = undoBulkCalls(
      [
        member({ id: "1" }),
        member({ id: "2" }),
        member({ id: "3", context_inclusion: "full", overridden: ["context_inclusion"] }),
      ],
      ["context_inclusion"],
    );
    expect(calls).toEqual([
      { memberIds: ["1", "2"], set: { context_inclusion: null } },
      { memberIds: ["3"], set: { context_inclusion: "full" } },
    ]);
  });
});

describe("organization scope", () => {
  it("is taken by every visible row, the other settings by connected rows only", () => {
    const home = member({ role: "member" });
    expect(isVisible(home)).toBe(true);
    expect(takesSetting(home, "organization_scope")).toBe(true);
    expect(takesSetting(home, "context_inclusion")).toBe(false);
    expect(takesSetting(member({ name: "" }), "organization_scope")).toBe(false);
  });

  it("reads the primary first, then the monitored set or each listed organization", () => {
    expect(scopeTags(undefined, ORGS)).toEqual([{ text: "Acme", translate: false }]);
    // The monitored scope is one whole tag: a second one would fold it into +N (UX round 3, B1).
    expect(scopeTags({ mode: "monitored" }, ORGS)).toEqual([{ text: "All monitored organizations", translate: true }]);
    expect(scopeTags({ mode: "list", organization_ids: ["t2", "gone"] }, ORGS)).toEqual([
      { text: "Acme", translate: false },
      { text: "Partner", translate: false },
      { text: "An organization not found", translate: true },
    ]);
    expect(scopeTags({ mode: "primary" }, [])).toEqual([{ text: "Own organization", translate: true }]);
  });

  it("offers the active tracked organizations only", () => {
    expect(trackedOrganizations(ORGS).map((o) => o.id)).toEqual(["t1", "t2"]);
  });

  it("answers monitored over a list, and the primary for no pick", () => {
    expect(scopeFromPicks(true, ["t1"])).toEqual({ mode: "monitored" });
    expect(scopeFromPicks(false, ["t1", "t1", "t2"])).toEqual({ mode: "list", organization_ids: ["t1", "t2"] });
    expect(scopeFromPicks(false, [])).toEqual({ mode: "primary" });
  });

  it("compares a list as a set", () => {
    expect(sameScope({ mode: "list", organization_ids: ["a", "b"] }, { mode: "list", organization_ids: ["b", "a"] })).toBe(true);
    expect(sameScope(undefined, { mode: "primary" })).toBe(true);
    expect(sameScope({ mode: "monitored" }, { mode: "primary" })).toBe(false);
  });

  it("counts the home rows in the impact of a scope default", () => {
    const rows = [member({ id: "c" }), member({ id: "h", role: "member", overridden: ["organization_scope"] }), member({ id: "x", name: "" })];
    expect(defaultImpact(rows, "organization_scope")).toEqual({ applies: 1, keep: 1 });
  });

  it("resets a home row's scope alone and every setting of a connected row", () => {
    expect(resetCalls([member({ id: "c" }), member({ id: "h", role: "member" })])).toEqual([
      { memberIds: ["c"], set: { context_inclusion: null, collaboration_instruction: null, organization_scope: null } },
      { memberIds: ["h"], set: { organization_scope: null } },
    ]);
  });

  it("undoes a scope change back to the row's own scope or to the fleet default", () => {
    const own = { mode: "list" as const, organization_ids: ["t1"] };
    const calls = undoBulkCalls(
      [member({ id: "1" }), member({ id: "2", organization_scope: own, overridden: ["organization_scope"] })],
      ["organization_scope"],
    );
    expect(calls).toEqual([
      { memberIds: ["1"], set: { organization_scope: null } },
      { memberIds: ["2"], set: { organization_scope: own } },
    ]);
  });
});

describe("settleAll", () => {
  it("reports once, after every call settled, with no refusal", async () => {
    const done: number[] = [];
    const outcome = await settleAll([1, 2, 3], async (n) => {
      await new Promise((resolve) => setTimeout(resolve, (4 - n) * 5));
      done.push(n);
    });
    expect(outcome).toBeNull();
    expect(done.sort()).toEqual([1, 2, 3]);
  });

  it("waits for every call and reports the first refusal once", async () => {
    const done: number[] = [];
    const outcome = await settleAll([1, 2, 3], async (n) => {
      done.push(n);
      if (n !== 2) throw new Error(`refused ${n}`);
    });
    expect(done).toEqual([1, 2, 3]);
    expect((outcome!.failed as Error).message).toBe("refused 1");
  });
});

describe("labels", () => {
  it("names each reset with the field in lower case inside the sentence", () => {
    expect(RESET_SETTING_LABELS.context_inclusion).toBe("Reset conversation shared to the fleet default");
    expect(RESET_SETTING_LABELS.collaboration_instruction).toBe("Reset collaboration instruction to the fleet default");
    expect(RESET_SETTING_LABELS.organization_scope).toBe("Reset organizations to the fleet default");
  });

  it("says Nothing, never No context, and never Full alone", () => {
    expect(CONTEXT_INCLUSION_LABELS.none).toBe("Nothing");
    expect(Object.values(CONTEXT_INCLUSION_SHORT_LABELS)).not.toContain("Full");
  });

  it("shows the first line of an instruction", () => {
    expect(firstLine("Call it for triage\nsecond line")).toBe("Call it for triage");
    expect(firstLine("")).toBe("");
  });
});
