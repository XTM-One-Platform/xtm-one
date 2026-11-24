// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { deletableRows, fieldEditable, isSyncedRow, nameProblem, newAssetRow, recordRefs, rowPatches } from "./inventory-entries";

const hand = { id: "r1", source: "manual", external_id: "5f0c" };
const synced = { id: "r2", source: "intune", external_id: "dev-1" };

describe("inventory entries (#1367, points 2 and 3; row APIs #1543)", () => {
  it("tells a synced row from a hand row", () => {
    expect(isSyncedRow(hand)).toBe(false);
    expect(isSyncedRow(synced)).toBe(true);
    expect(isSyncedRow({ source: "intune", external_id: "" })).toBe(false);
  });

  it("locks every field a source sends, keeps the local ones", () => {
    expect(fieldEditable("assets_exposure", synced, "criticality")).toBe(false);
    expect(fieldEditable("assets_exposure", synced, "name")).toBe(false);
    expect(fieldEditable("assets_exposure", synced, "owner")).toBe(true);
    expect(fieldEditable("assets_exposure", synced, "group")).toBe(true);
    expect(fieldEditable("asset_groups", { source: "openaev", external_id: "g1" }, "description")).toBe(true);
    expect(fieldEditable("assets_exposure", hand, "criticality")).toBe(true);
  });

  it("addresses the records of canonical assets by id, skipping a ref without one", () => {
    const assets = [{ records: [{ ...hand }, { source: "csv", external_id: "x" }] }, { records: [{ ...synced }] }, {}];
    expect(recordRefs(assets).map((r) => r.id)).toEqual(["r1", "r2"]);
  });

  it("patches only what a row allows and counts the locked rows", () => {
    const out = rowPatches("assets_exposure", [hand, synced], { criticality: "low" });
    expect(out.patches).toEqual([{ id: "r1", patch: { criticality: "low" } }]);
    expect(out.locked).toBe(1);
    const owner = rowPatches("assets_exposure", [hand, synced], { owner: "SOC", criticality: "low" });
    expect(owner.patches).toEqual([
      { id: "r1", patch: { owner: "SOC", criticality: "low" } },
      { id: "r2", patch: { owner: "SOC" } },
    ]);
    expect(owner.locked).toBe(0);
  });

  it("never deletes a synced row", () => {
    expect(deletableRows([hand, synced])).toEqual({ ids: ["r1"], locked: 1 });
  });

  it("refuses an empty name and a name another group holds", () => {
    const groups = [{ id: "g1", name: "Servers" }];
    expect(nameProblem(groups, " ")).toBe("empty");
    expect(nameProblem(groups, "SERVERS")).toBe("taken");
    expect(nameProblem(groups, "servers", "g1")).toBeUndefined();
  });

  it("gives a new hand asset the defaults", () => {
    expect(newAssetRow({ name: " build-01 " })).toMatchObject({ name: "build-01", category: "generic_asset", criticality: "unknown", source: "manual" });
  });
});
