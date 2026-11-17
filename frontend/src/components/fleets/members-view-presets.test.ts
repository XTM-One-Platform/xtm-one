import { describe, expect, it } from "vitest";

import type { FleetMember } from "@/hooks/api/useFleetApi";

import {
  BUILTIN_PRESET_NAMES,
  presetCalls,
  presetDefaultsChange,
  presetName,
  presetSettings,
  undoBulkCalls,
} from "./members-view";

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

/** The built-in "All monitored organizations, nothing" (backend BUILTIN_PRESETS). */
const MONITORED_NOTHING = { context_inclusion: "none", organization_scope: { mode: "monitored" } } as const;

describe("presetName", () => {
  it("translates a built-in by its id and shows a company preset as typed", () => {
    expect(presetName({ id: "builtin:monitored-nothing", name: "ignored", builtin: true })).toEqual({
      text: "All monitored organizations, nothing",
      translate: true,
    });
    expect(presetName({ id: "p1", name: "SOC handoff", builtin: false })).toEqual({ text: "SOC handoff", translate: false });
  });

  it("falls back to the server's name for a built-in id it does not know", () => {
    expect(presetName({ id: "builtin:later", name: "Later", builtin: true })).toEqual({ text: "Later", translate: false });
  });

  it("names the three built-ins of 10.6", () => {
    expect(Object.keys(BUILTIN_PRESET_NAMES)).toEqual(["builtin:own-summary", "builtin:monitored-nothing", "builtin:own-full"]);
  });
});

describe("presetSettings", () => {
  it("lists the settings a preset sets, in column order", () => {
    expect(presetSettings({ organization_scope: { mode: "primary" }, context_inclusion: "full" })).toEqual(["context_inclusion", "organization_scope"]);
    expect(presetSettings({ collaboration_instruction: "Cite sources" })).toEqual(["collaboration_instruction"]);
  });
});

describe("presetCalls", () => {
  it("writes every value on connected rows and the scope alone on home rows", () => {
    const rows = [member({ id: "c1" }), member({ id: "h1", role: "member", name: "Home" }), member({ id: "c2" })];
    expect(presetCalls(rows, MONITORED_NOTHING)).toEqual([
      { memberIds: ["c1", "c2"], set: { context_inclusion: "none", organization_scope: { mode: "monitored" } } },
      { memberIds: ["h1"], set: { organization_scope: { mode: "monitored" } } },
    ]);
  });

  it("makes no call for home rows when the preset sets no scope", () => {
    const rows = [member({ id: "c1" }), member({ id: "h1", role: "member", name: "Home" })];
    expect(presetCalls(rows, { context_inclusion: "summary" })).toEqual([{ memberIds: ["c1"], set: { context_inclusion: "summary" } }]);
  });

  it("skips a row not shared with the reader", () => {
    expect(presetCalls([member({ id: "x", name: "" })], MONITORED_NOTHING)).toEqual([]);
  });

  it("undoes back to the previous values, override markers included", () => {
    const rows = [
      member({ id: "c1" }),
      member({ id: "c2", context_inclusion: "full", overridden: ["context_inclusion"] }),
    ];
    const [call] = presetCalls(rows, MONITORED_NOTHING);
    const undo = undoBulkCalls(rows, Object.keys(call.set) as ("context_inclusion" | "organization_scope")[]);
    expect(undo).toEqual([
      { memberIds: ["c1"], set: { context_inclusion: null, organization_scope: null } },
      { memberIds: ["c2"], set: { context_inclusion: "full", organization_scope: null } },
    ]);
  });
});

describe("presetDefaultsChange", () => {
  it("patches the fields the preset sets and keeps their previous values for the Undo", () => {
    expect(presetDefaultsChange({ context_inclusion: "summary", collaboration_instruction: "Keep" }, MONITORED_NOTHING)).toEqual({
      patch: { context_inclusion: "none", organization_scope: { mode: "monitored" } },
      previous: { context_inclusion: "summary", organization_scope: { mode: "primary" } },
    });
  });

  it("reads absent defaults as the stored fallbacks", () => {
    expect(presetDefaultsChange(undefined, { collaboration_instruction: "New" })).toEqual({
      patch: { collaboration_instruction: "New" },
      previous: { collaboration_instruction: "" },
    });
  });
});
