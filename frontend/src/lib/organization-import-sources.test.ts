// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import { filterValueLabel, filtersSummary, keepsSourceOrder } from "@/lib/organization-import-filters";
import { IMPORT_SOURCE_NAMES, IMPORT_SOURCE_PROVIDER, PLATFORM_NAMES, importSourcesOfProvider } from "@/lib/organization-profile";

const t = (text: string, values?: Record<string, string | number>) =>
  text.includes("plural") ? `within ${values?.count} days` : Object.entries(values ?? {}).reduce((out, [k, v]) => out.replace(`{${k}}`, String(v)), text);

describe("Intune and Defender as import sources (#1251)", () => {
  it("names both sources and reads each through its own integration provider, as the backend does", () => {
    expect(IMPORT_SOURCE_NAMES.intune).toBe("Microsoft Intune");
    expect(IMPORT_SOURCE_NAMES.defender).toBe("Microsoft Defender for Endpoint");
    expect(IMPORT_SOURCE_PROVIDER.intune).toBe("intune");
    expect(IMPORT_SOURCE_PROVIDER.defender).toBe("defender");
  });

  it("keeps the Filigran platforms list unchanged for dashboards and drafts", () => {
    expect(Object.keys(PLATFORM_NAMES)).toEqual(["opencti", "openaev", "opencrq"]);
  });

  it("opens Intune from an Intune integration, Defender from a Defender one, a platform from itself, nothing from others", () => {
    expect(importSourcesOfProvider("intune")).toEqual(["intune"]);
    expect(importSourcesOfProvider("defender")).toEqual(["defender"]);
    // The Outlook and Teams integration carries no device permission: it opens neither.
    expect(importSourcesOfProvider("microsoft")).toEqual([]);
    expect(importSourcesOfProvider("openaev")).toEqual(["openaev"]);
    // GitHub and GitLab integrations open their repositories as a code source.
    expect(importSourcesOfProvider("github")).toEqual(["github"]);
    expect(importSourcesOfProvider("gitlab")).toEqual(["gitlab"]);
    expect(importSourcesOfProvider("slack")).toEqual([]);
  });

  it("words the Microsoft filter values and summarizes a sync's filters", () => {
    expect(filterValueLabel(t, "compliance", "noncompliant")).toBe("Not compliant");
    expect(filterValueLabel(t, "health_status", "ImpairedCommunication")).toBe("Impaired communication");
    expect(filterValueLabel(t, "operating_system", "Windows")).toBe("Windows");
    expect(filterValueLabel(t, "os_platform", "Windows10")).toBe("Windows 10");
    expect(filterValueLabel(t, "os_platform", "WindowsServer2022")).toBe("Windows Server 2022");
    expect(filterValueLabel(t, "os_platform", "macOS")).toBe("macOS");
    expect(filterValueLabel(t, "last_seen_days", "30")).toBe("within 30 days");
    expect(filtersSummary(t, "en", { compliance: ["compliant"], exposure_level: ["High"] })).toBe("Compliance: Compliant; Exposure level: High");
  });

  it("keeps ServiceNow's operational status in its own order, Operational first and Retired last, in every language", () => {
    expect(keepsSourceOrder("operational_status")).toBe(true);
    expect(keepsSourceOrder("criticality")).toBe(true);
    expect(keepsSourceOrder("operating_system")).toBe(false);
    expect(keepsSourceOrder("ci_class")).toBe(false);
    // The backend sends the choice values 1 to 6 (FILTER_VALUES); unsorted, the picker shows them in this order.
    expect(["1", "2", "3", "4", "5", "6"].map((value) => filterValueLabel(t, "operational_status", value))).toEqual([
      "Operational",
      "Non-operational",
      "Repair in progress",
      "DR standby",
      "Ready",
      "Retired (ServiceNow status)",
    ]);
  });
});
