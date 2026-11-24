// SPDX-License-Identifier: Apache-2.0

/** The marks of CTEM > Inventory (#1367, point 8): every category has a glyph, OS families map to vendor marks. */
import { describe, expect, it } from "vitest";
import type { AssetSourceRecord } from "@/hooks/api/useOrganizationAssetsApi";
import { RUNNER_PLATFORM_CONFIG } from "@/lib/constants";
import { fieldText, identifierValues } from "./AssetOverview";
import { CATEGORY_ICONS, SUBCATEGORY_ICONS, categoryIcon, osLogo } from "./inventory-icons";

describe("inventory marks", () => {
  it("gives every OpenAEV category a glyph and prefers the subcategory's", () => {
    for (const category of ["host", "web_application", "mobile_device", "cloud_resource", "identity", "generic_asset"]) {
      expect(CATEGORY_ICONS[category]).toBeTruthy();
    }
    expect(categoryIcon("host", "laptop")).toBe(SUBCATEGORY_ICONS.laptop);
    expect(categoryIcon("host", "")).toBe(CATEGORY_ICONS.host);
    expect(categoryIcon("nonsense", null)).toBe("box");
  });

  it("maps OS families to the vendor marks the app ships, the Apple mark for iOS and iPadOS too", () => {
    expect(osLogo("Windows")).toBe(RUNNER_PLATFORM_CONFIG.windows.logo);
    expect(osLogo("macOS")).toBe(RUNNER_PLATFORM_CONFIG.macos.logo);
    expect(osLogo("iOS")).toBe(RUNNER_PLATFORM_CONFIG.macos.logo);
    expect(osLogo("iPadOS")).toBe(RUNNER_PLATFORM_CONFIG.macos.logo);
    expect(osLogo("Ubuntu")).toBe(RUNNER_PLATFORM_CONFIG.linux.logo);
    expect(osLogo("Android")).toBeNull();
    expect(osLogo("")).toBeNull();
  });
});

describe("asset overview values", () => {
  const record = (identifiers: Record<string, unknown>): AssetSourceRecord => ({
    source: "intune",
    external_id: "x",
    identifiers,
    fields: {},
    last_seen: null,
  });

  it("collects each identifier kind once across records, in record order", () => {
    const records = [record({ mac: ["aa:bb", "cc:dd"], ip: [] }), record({ mac: ["cc:dd", "ee:ff"], hostname: "pc-1" })];
    expect(identifierValues(records, "mac")).toEqual(["aa:bb", "cc:dd", "ee:ff"]);
    expect(identifierValues(records, "hostname")).toEqual(["pc-1"]);
    expect(identifierValues(records, "ip")).toEqual([]);
  });

  it("hides an empty or unknown field", () => {
    expect(fieldText({ criticality: { value: "unknown" } }, "criticality")).toBe("");
    expect(fieldText({ owner: { value: "Alice" } }, "owner")).toBe("Alice");
    expect(fieldText({}, "owner")).toBe("");
  });
});
