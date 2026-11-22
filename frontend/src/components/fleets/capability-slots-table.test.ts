import { describe, expect, it } from "vitest";

import type { FleetCapabilityType, FleetCapabilityTypeMember } from "@/hooks/api/useFleetApi";
import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";
import { sortRows } from "@/lib/table-rows";

import {
  CAPABILITIES_OPEN_BY_DEFAULT,
  CAPABILITY_ICONS,
  FALLBACK_CAPABILITY_ICON,
  FLEET_DEFAULTS_OPEN_BY_DEFAULT,
  NO_INTEGRATION_VALUE,
  STATUS_LABEL_MAX_CHARS,
  TYPE_STATE_LABELS,
  builtinValue,
  capabilityIcon,
  customMembers,
  idsForPick,
  instructionSummaryKey,
  integrationIds,
  overwritableCustomMembers,
  pickerValue,
  toggledIds,
  typeCounts,
  typeEditable,
  typePickerValue,
  typeSearchFields,
  typeStateRank,
  unsetMembers,
} from "./capability-slots-table";

const JIRA = { integration_id: "i1", provider: "jira", name: "Jira ITS" };
const SNOW = { integration_id: "i2", provider: "servicenow", name: "ServiceNow" };

function member(id: string, overrides: Partial<FleetCapabilityTypeMember> = {}): FleetCapabilityTypeMember {
  return { member_id: id, agent_id: `agent-${id}`, name: id, role_label: "Mobilization", can_edit: true, bound: [], custom: false, ...overrides };
}

function type(capability: string, state: FleetCapabilityType["state"], overrides: Partial<FleetCapabilityType> = {}): FleetCapabilityType {
  return { capability, label: capability, state, choice: [], candidates: [], members: [member("m1")], builtin: null, ...overrides };
}

const INVENTORY = { key: "xtm_one_inventory", label: "XTM One inventory (built in)", always_on: true };

describe("capability types table (#1201)", () => {
  it("opens Capabilities and folds Fleet defaults by default", () => {
    expect(CAPABILITIES_OPEN_BY_DEFAULT).toBe(true);
    expect(FLEET_DEFAULTS_OPEN_BY_DEFAULT).toBe(false);
  });

  it("sorts Not bound first, then Partial, None connected, Connected", () => {
    const rows = [type("a", "connected"), type("b", "none_connected"), type("c", "partial"), type("d", "not_bound")];
    expect(sortRows(rows, typeStateRank, "asc").map((row) => row.state)).toEqual(["not_bound", "partial", "none_connected", "connected"]);
  });

  it("searches the capability, its built-in option, members, roles and bound integrations", () => {
    const row = type("asset_inventory", "partial", { builtin: INVENTORY, members: [member("Mobilizer", { bound: [JIRA] })] });
    expect(typeSearchFields(row, "Asset inventory")).toEqual(
      expect.arrayContaining(["Asset inventory", "asset_inventory", "XTM One inventory (built in)", "Mobilizer", "Mobilization", "Jira ITS", "jira"]),
    );
  });

  it("counts the connected types for the summary", () => {
    expect(typeCounts([type("a", "connected"), type("b", "partial"), type("c", "connected")])).toEqual({ connected: 2, total: 3 });
  });

  it("names the custom members and asks only for the ones the reader may edit", () => {
    const row = type("ticketing", "connected", {
      members: [member("m1", { custom: true }), member("m2"), member("m3", { custom: true, can_edit: false })],
    });
    expect(customMembers(row).map((m) => m.member_id)).toEqual(["m1", "m3"]);
    expect(overwritableCustomMembers(row).map((m) => m.member_id)).toEqual(["m1"]);
    expect(typeEditable(row)).toBe(true);
    expect(typeEditable(type("x", "connected", { members: [member("m1", { can_edit: false })] }))).toBe(false);
  });

  it("never writes the built-in option as an integration", () => {
    const row = type("asset_inventory", "connected", { builtin: INVENTORY });
    expect(builtinValue(row)).toBe("builtin:xtm_one_inventory");
    expect(builtinValue(type("ticketing", "connected"))).toBe("");
    expect(idsForPick("builtin:xtm_one_inventory")).toEqual([]);
    expect(idsForPick("")).toEqual([]);
    expect(idsForPick("i1")).toEqual(["i1"]);
  });

  it("shows the bound integration, else the built-in option, in a single-choice picker", () => {
    const row = type("asset_inventory", "connected", { builtin: INVENTORY });
    expect(pickerValue(row, [SNOW])).toBe("i2");
    expect(pickerValue(row, [])).toBe("builtin:xtm_one_inventory");
    expect(pickerValue(type("ticketing", "not_bound"), [])).toBe("");
  });

  it("toggles a channel of a multi-channel capability and keeps the built-in option on", () => {
    expect(toggledIds(["i2"], "i1")).toEqual(["i1", "i2"]);
    expect(toggledIds(["i1", "i2"], "i1")).toEqual(["i2"]);
    expect(toggledIds(["i2"], "builtin:in_app")).toEqual(["i2"]);
    expect(integrationIds([SNOW, JIRA])).toEqual(["i1", "i2"]);
  });

  it("gives every catalog capability its own icon and an unknown one the fallback", () => {
    for (const capability of ["ticketing", "notification", "edr_xdr", "scanner", "easm", "vulnerability_intelligence", "asset_inventory", "siem"])
      expect(CAPABILITY_ICONS[capability]).toBeTruthy();
    expect(new Set(Object.values(CAPABILITY_ICONS)).size).toBe(Object.keys(CAPABILITY_ICONS).length);
    expect(capabilityIcon("quantum_radar")).toBe(FALLBACK_CAPABILITY_ICON);
  });

  it("sizes the Status column for the longest state label of the four catalogs (UX r1 B5 of #1209)", () => {
    for (const catalog of [en, fr, es, ja] as Record<string, string>[])
      for (const key of Object.values(TYPE_STATE_LABELS)) expect((catalog[key] ?? key).length).toBeLessThanOrEqual(STATUS_LABEL_MAX_CHARS);
  });

  it("names the members a Partial type leaves unset and shows no picker value for it (UX r1 B2 of #1209)", () => {
    const partial = type("ticketing", "partial", { choice: [JIRA], members: [member("a", { bound: [JIRA] }), member("b")] });
    expect(unsetMembers(partial).map((m) => m.member_id)).toEqual(["b"]);
    expect(typePickerValue(partial)).toBe("");
    expect(typePickerValue({ ...partial, state: "connected", members: [member("a", { bound: [JIRA] })] })).toBe("i1");
    expect(unsetMembers(type("asset_inventory", "connected", { builtin: INVENTORY }))).toEqual([]);
  });

  it("writes no integration for the member's No integration option (UX r1 B1 of #1209)", () => {
    expect(idsForPick(NO_INTEGRATION_VALUE)).toEqual([]);
  });

  it("reads a blank collaboration instruction as none in the folded summary", () => {
    expect(instructionSummaryKey("")).toBe("none");
    expect(instructionSummaryKey("   ")).toBe("none");
    expect(instructionSummaryKey(undefined)).toBe("none");
    expect(instructionSummaryKey("Call the CTI desk first")).toBe("set");
  });
});
