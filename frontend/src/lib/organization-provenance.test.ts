// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import { columnEditable, exclusionsBySync, isPlatformSource, LOCAL_FIELDS, sourceKind, sourcePlatform, syncedCount } from "./organization-provenance";

describe("organization row provenance (#1134)", () => {
  it("tells a platform row from a hand or agent row", () => {
    for (const source of ["opencrq", "openaev", "OpenCTI "]) expect(isPlatformSource(source)).toBe(true);
    for (const source of [undefined, "", "manual", "csv", "api", "agent:123"]) expect(isPlatformSource(source)).toBe(false);
  });

  it("names the kind and the platform of a source", () => {
    expect(sourceKind(undefined)).toBe("manual");
    expect(sourceKind("csv")).toBe("csv");
    expect(sourceKind("agent:a1")).toBe("agent");
    expect(sourceKind("opencrq")).toBe("platform");
    expect(sourcePlatform("opencrq")).toBe("opencrq");
    expect(sourcePlatform("manual")).toBe("");
  });

  it("locks a platform row but for its local enrichment fields", () => {
    expect(columnEditable("manual", "name", LOCAL_FIELDS.crown_jewels)).toBe(true);
    expect(columnEditable("opencrq", "name", LOCAL_FIELDS.crown_jewels)).toBe(false);
    expect(columnEditable("opencrq", "criticality", LOCAL_FIELDS.crown_jewels)).toBe(false);
    expect(columnEditable("opencrq", "business_service", LOCAL_FIELDS.crown_jewels)).toBe(true);
    expect(columnEditable("openaev", "owner", LOCAL_FIELDS.assets_exposure)).toBe(true);
  });

  it("counts synced rows and groups their exclusions by sync", () => {
    const rows = [
      { source: "opencrq", external_id: "a", sync_id: "s1" },
      { source: "opencrq", external_id: "b", sync_id: "s1" },
      { source: "openaev", external_id: "c", sync_id: "s2" },
      { source: "opencrq", external_id: "d", sync_id: "" },
      { source: "manual", external_id: "e", sync_id: "s1" },
    ];
    expect(syncedCount(rows)).toBe(4);
    expect(Object.fromEntries(exclusionsBySync(rows))).toEqual({ s1: ["a", "b"], s2: ["c"] });
  });
});
