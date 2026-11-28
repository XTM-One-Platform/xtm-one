import { describe, expect, it } from "vitest";

import type { FleetCapabilityType, FleetCapabilityTypeMember } from "@/hooks/api/useFleetApi";
import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";
import { sortRows } from "@/lib/table-rows";

import {
  BLOCKED_REASON_LINES,
  BLOCKED_REASON_SHORT,
  CAPABILITIES_OPEN_BY_DEFAULT,
  CAPABILITY_ICONS,
  FALLBACK_CAPABILITY_ICON,
  FLEET_DEFAULTS_OPEN_BY_DEFAULT,
  CAPABILITY_CELL_CHROME,
  SECONDARY_COLUMNS_HIDE_BELOW_PX,
  STATUS_COLUMN_WIDTH,
  STATUS_LABEL_MAX_CHARS,
  TYPE_STATE_LABELS,
  blockedFixPath,
  builtinValue,
  capabilityIcon,
  firstBlocked,
  flexibleColumnWidth,
  customMembers,
  isBuiltinValue,
  multiChannelSelection,
  instructionSummaryKey,
  integrationIds,
  overwritableCustomMembers,
  toggledIds,
  typeCounts,
  typeEditable,
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

  it("sorts Not bound first, then Partial, Not usable, None connected, Connected", () => {
    const rows = [type("a", "connected"), type("b", "none_connected"), type("c", "partial"), type("d", "not_bound"), type("e", "not_usable")];
    expect(sortRows(rows, typeStateRank, "asc").map((row) => row.state)).toEqual([
      "not_bound",
      "partial",
      "not_usable",
      "none_connected",
      "connected",
    ]);
  });

  it("names a connected integration the members cannot take, never 'Connect one' (#1373)", () => {
    const blocked = { ...JIRA, reason: "not_company_managed" as const };
    const row = type("ticketing", "not_usable", { blocked: [blocked] });
    expect(firstBlocked(row)).toEqual(blocked);
    expect(firstBlocked(type("ticketing", "none_connected"))).toBeNull();
    expect(blockedFixPath(blocked)).toBe("/integrations/i1");
    for (const catalog of [en, fr, es, ja] as Record<string, string>[])
      // The row names the integration in words, "YouTrack · Not company-managed", +N for the rest (UX r1 B2 of #1376).
      for (const key of [...Object.values(BLOCKED_REASON_LINES), ...Object.values(BLOCKED_REASON_SHORT), "{name} · {reason}", "+{count}"])
        expect(catalog[key], `${key} is in every catalog`).toBeTruthy();
  });

  it("shows the always-on built-in channel checked first, never the empty placeholder (#1373)", () => {
    const notification = type("notification", "connected", { builtin: { key: "in_app", label: "In XTM One", always_on: true } });
    expect(multiChannelSelection(notification, [])).toEqual(["builtin:in_app"]);
    expect(multiChannelSelection(notification, [SNOW, JIRA])).toEqual(["builtin:in_app", "i1", "i2"]);
    expect(multiChannelSelection(type("notification", "not_bound"), [JIRA])).toEqual(["i1"]);
    expect(isBuiltinValue("builtin:in_app")).toBe(true);
    expect(isBuiltinValue("i1")).toBe(false);
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
    expect(toggledIds(["i1"], "builtin:xtm_one_inventory")).toEqual(["i1"]);
  });

  it("shows the built-in option locked first, then every bound integration, on asset inventory (#2424)", () => {
    const row = type("asset_inventory", "connected", { builtin: INVENTORY });
    expect(multiChannelSelection(row, [SNOW])).toEqual(["builtin:xtm_one_inventory", "i2"]);
    expect(multiChannelSelection(row, [])).toEqual(["builtin:xtm_one_inventory"]);
    expect(multiChannelSelection(type("ticketing", "not_bound"), [])).toEqual([]);
  });

  it("adds a second integration to a non-notification capability and removes one of two (#2424)", () => {
    const ticketing = type("ticketing", "connected", { choice: [JIRA], candidates: [JIRA, SNOW] });
    const two = toggledIds(integrationIds(ticketing.choice), "i2");
    expect(two).toEqual(["i1", "i2"]);
    expect(multiChannelSelection(ticketing, [JIRA, SNOW])).toEqual(["i1", "i2"]);
    expect(toggledIds(two, "i1")).toEqual(["i2"]);
    expect(toggledIds(["i2"], "i2")).toEqual([]);
  });

  it("toggles a channel of a multi-channel capability and keeps the built-in option on", () => {
    expect(toggledIds(["i2"], "i1")).toEqual(["i1", "i2"]);
    expect(toggledIds(["i1", "i2"], "i1")).toEqual(["i2"]);
    expect(toggledIds(["i2"], "builtin:in_app")).toEqual(["i2"]);
    expect(integrationIds([SNOW, JIRA])).toEqual(["i1", "i2"]);
  });

  it("gives every catalog capability its own icon and an unknown one the fallback", () => {
    for (const capability of [
      "ticketing",
      "notification",
      "edr_xdr",
      "scanner",
      "application_security",
      "source_code",
      "easm",
      "vulnerability_intelligence",
      "asset_inventory",
      "siem",
      "search_engine",
    ])
      expect(CAPABILITY_ICONS[capability]).toBeTruthy();
    expect(new Set(Object.values(CAPABILITY_ICONS)).size).toBe(Object.keys(CAPABILITY_ICONS).length);
    expect(capabilityIcon("quantum_radar")).toBe(FALLBACK_CAPABILITY_ICON);
  });

  it("sizes the Status column for the longest state label of the four catalogs (UX r1 B5 of #1209)", () => {
    for (const catalog of [en, fr, es, ja] as Record<string, string>[])
      for (const key of Object.values(TYPE_STATE_LABELS)) expect((catalog[key] ?? key).length).toBeLessThanOrEqual(STATUS_LABEL_MAX_CHARS);
  });

  it("keeps the Status column at the widest badge, not wider (UX r2 B1 of #1209)", () => {
    // "Aucune connectée" was measured as a 126 px badge: the column is that badge, padded, at most 8 px more.
    expect(STATUS_COLUMN_WIDTH).toBeGreaterThanOrEqual(126 + 24);
    expect(STATUS_COLUMN_WIDTH).toBeLessThanOrEqual(126 + 24 + 8);
  });

  // The capability labels of backend/app/services/fleet_capability_slots.py (CAPABILITIES).
  const CAPABILITY_LABELS = [
    "Ticketing",
    "Notification",
    "EDR / XDR",
    "Vulnerability scanner",
    "Application security",
    "Source code",
    "External attack surface",
    "Vulnerability intelligence",
    "Asset inventory",
    "SIEM",
    "Search engine",
  ];
  // 14 px body text: 7.2 px a character is above the measured average of the four catalogs.
  const textWidth = (text: string) => text.length * 7.2;
  // The table's width at a 1080 px window (sidebar, page and card padding taken), and in the
  // 960 px setup dialog at any window.
  const TABLE_AT_1080 = 812;
  const TABLE_IN_DIALOG = 896;

  it("fits the longest capability name of each catalog at 1080 and in the setup dialog (UX r2 B1 of #1209)", () => {
    for (const tableWidth of [TABLE_AT_1080, TABLE_IN_DIALOG]) {
      expect(tableWidth).toBeLessThan(SECONDARY_COLUMNS_HIDE_BELOW_PX);
      const room = flexibleColumnWidth(tableWidth) - CAPABILITY_CELL_CHROME;
      for (const catalog of [en, fr, es, ja] as Record<string, string>[])
        for (const label of CAPABILITY_LABELS) {
          expect(catalog[label], `${label} is in every catalog`).toBeTruthy();
          expect(textWidth(catalog[label]), `${catalog[label]} at ${tableWidth}`).toBeLessThanOrEqual(room);
        }
    }
  });

  it("fits the French unset-members badge in the Integration column where it is drawn (UX r2 B2 of #1209)", () => {
    const badge = (fr as Record<string, string>)["{count, plural, one {# member not set} other {# members not set}}"];
    expect(badge).toContain("membre non défini");
    // "12 membres non définis" at 12 px (6.9 px a character) and the badge's 8 px a side, in a cell padded 12 px a side.
    const widest = "12 membres non définis".length * 6.9 + 16;
    expect(widest).toBeLessThanOrEqual(flexibleColumnWidth(SECONDARY_COLUMNS_HIDE_BELOW_PX) - 24);
  });

  it("names the members a Partial type leaves unset (UX r1 B2 of #1209)", () => {
    const partial = type("ticketing", "partial", { choice: [JIRA], members: [member("a", { bound: [JIRA] }), member("b")] });
    expect(unsetMembers(partial).map((m) => m.member_id)).toEqual(["b"]);
    expect(unsetMembers(type("asset_inventory", "connected", { builtin: INVENTORY }))).toEqual([]);
  });

  it("reads a blank collaboration instruction as none in the folded summary", () => {
    expect(instructionSummaryKey("")).toBe("none");
    expect(instructionSummaryKey("   ")).toBe("none");
    expect(instructionSummaryKey(undefined)).toBe("none");
    expect(instructionSummaryKey("Call the CTI desk first")).toBe("set");
  });
});
