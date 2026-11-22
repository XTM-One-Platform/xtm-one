// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import { filterValueLabel, filtersSummary } from "@/lib/organization-import-filters";
import { IMPORT_SOURCE_NAMES, IMPORT_SOURCE_PROVIDER, PLATFORM_NAMES, importSourcesOfProvider } from "@/lib/organization-profile";

const t = (text: string, values?: Record<string, string | number>) =>
  text.includes("plural") ? `within ${values?.count} days` : Object.entries(values ?? {}).reduce((out, [k, v]) => out.replace(`{${k}}`, String(v)), text);

describe("Intune and Defender as import sources (#1251)", () => {
  it("names both sources and reads their logo from the Microsoft integration", () => {
    expect(IMPORT_SOURCE_NAMES.intune).toBe("Microsoft Intune");
    expect(IMPORT_SOURCE_NAMES.defender).toBe("Microsoft Defender for Endpoint");
    expect(IMPORT_SOURCE_PROVIDER.intune).toBe("microsoft");
    expect(IMPORT_SOURCE_PROVIDER.defender).toBe("microsoft");
  });

  it("keeps the Filigran platforms list unchanged for dashboards and drafts", () => {
    expect(Object.keys(PLATFORM_NAMES)).toEqual(["opencti", "openaev", "opencrq"]);
  });

  it("opens Intune and Defender from a Microsoft integration, a platform from itself, nothing from others", () => {
    expect(importSourcesOfProvider("microsoft")).toEqual(["intune", "defender"]);
    expect(importSourcesOfProvider("openaev")).toEqual(["openaev"]);
    expect(importSourcesOfProvider("github")).toEqual([]);
  });

  it("words the Microsoft filter values and summarizes a sync's filters", () => {
    expect(filterValueLabel(t, "compliance", "noncompliant")).toBe("Not compliant");
    expect(filterValueLabel(t, "health_status", "ImpairedCommunication")).toBe("Impaired communication");
    expect(filterValueLabel(t, "operating_system", "Windows")).toBe("Windows");
    expect(filterValueLabel(t, "last_seen_days", "30")).toBe("within 30 days");
    expect(filtersSummary(t, "en", { compliance: ["compliant"], exposure_level: ["High"] })).toBe("Compliance: Compliant; Exposure level: High");
  });
});
