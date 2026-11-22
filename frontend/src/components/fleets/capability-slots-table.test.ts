import { describe, expect, it } from "vitest";

import type { FleetCapabilitySlot, FleetMemberSlots } from "@/hooks/api/useFleetApi";
import { sortRows } from "@/lib/table-rows";

import {
  CAPABILITIES_OPEN_BY_DEFAULT,
  CAPABILITY_ICONS,
  FALLBACK_CAPABILITY_ICON,
  FLEET_DEFAULTS_OPEN_BY_DEFAULT,
  capabilityIcon,
  capabilityOptions,
  capabilitySlotRows,
  instructionSummaryKey,
  slotCounts,
  slotRowSearchFields,
  slotStateRank,
} from "./capability-slots-table";

function slot(capability: string, state: FleetCapabilitySlot["state"], bound: FleetCapabilitySlot["bound"] = []): FleetCapabilitySlot {
  return { capability, label: capability, state, bound, candidates: [] };
}

function member(id: string, name: string, slots: FleetCapabilitySlot[]): FleetMemberSlots {
  return { member_id: id, agent_id: `agent-${id}`, name, role_label: "Mobilization", can_edit: true, slots };
}

const MEMBERS = [
  member("m1", "Mobilizer", [
    slot("ticketing", "filled", [{ integration_id: "i1", provider: "jira", name: "Jira ITS" }]),
    slot("notification", "unavailable"),
  ]),
  member("m2", "Validator", [slot("edr_xdr", "empty"), slot("ticketing", "empty")]),
];

describe("capability slots table (#1128)", () => {
  it("opens Capabilities and folds Fleet defaults by default", () => {
    expect(CAPABILITIES_OPEN_BY_DEFAULT).toBe(true);
    expect(FLEET_DEFAULTS_OPEN_BY_DEFAULT).toBe(false);
  });

  it("builds one row per member x slot, keyed by both", () => {
    const rows = capabilitySlotRows(MEMBERS);
    expect(rows.map((row) => row.key)).toEqual(["m1:ticketing", "m1:notification", "m2:edr_xdr", "m2:ticketing"]);
    expect(rows[2].member.name).toBe("Validator");
    expect(capabilitySlotRows([])).toEqual([]);
  });

  it("sorts Not bound first, then None connected, then Connected", () => {
    const sorted = sortRows(capabilitySlotRows(MEMBERS), slotStateRank, "asc");
    expect(sorted.map((row) => row.slot.state)).toEqual(["empty", "empty", "unavailable", "filled"]);
  });

  it("searches capability, member, role and the bound integrations", () => {
    const [first] = capabilitySlotRows(MEMBERS);
    const fields = slotRowSearchFields(first, "Ticketing");
    expect(fields).toEqual(expect.arrayContaining(["Ticketing", "ticketing", "Mobilizer", "Mobilization", "Jira ITS", "jira"]));
  });

  it("offers each capability once in the capability filter", () => {
    expect(capabilityOptions(capabilitySlotRows(MEMBERS)).map((option) => option.value)).toEqual(["ticketing", "notification", "edr_xdr"]);
  });

  it("counts the connected slots for the summary", () => {
    expect(slotCounts(MEMBERS)).toEqual({ connected: 1, total: 4 });
  });

  it("gives every catalog capability its own icon and an unknown one the fallback", () => {
    for (const capability of ["ticketing", "notification", "edr_xdr", "scanner", "easm", "vulnerability_intelligence", "asset_inventory", "siem"])
      expect(CAPABILITY_ICONS[capability]).toBeTruthy();
    expect(new Set(Object.values(CAPABILITY_ICONS)).size).toBe(Object.keys(CAPABILITY_ICONS).length);
    expect(capabilityIcon("quantum_radar")).toBe(FALLBACK_CAPABILITY_ICON);
  });

  it("reads a blank collaboration instruction as none in the folded summary", () => {
    expect(instructionSummaryKey("")).toBe("none");
    expect(instructionSummaryKey("   ")).toBe("none");
    expect(instructionSummaryKey(undefined)).toBe("none");
    expect(instructionSummaryKey("Call the CTI desk first")).toBe("set");
  });
});
