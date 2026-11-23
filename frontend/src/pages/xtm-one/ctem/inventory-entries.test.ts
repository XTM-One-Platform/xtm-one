// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import type { AssetRow } from "@/lib/organization-profile";
import { assetRowIndexes, deleteRows, fieldEditable, isSyncedRow, nameProblem, newAssetRow, patchRows } from "./inventory-entries";

const hand = newAssetRow({ name: "build-01", criticality: "high" });
const synced: AssetRow = { ...newAssetRow({ name: "MacBook" }), source: "intune", external_id: "dev-1" };

describe("inventory entries (#1367, points 2 and 3)", () => {
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

  it("finds the rows of a canonical asset, a hand row by its position", () => {
    const rows = [hand, synced];
    expect(assetRowIndexes(rows, [{ source: "manual", external_id: "row-0" }])).toEqual([0]);
    expect(assetRowIndexes(rows, [{ source: "intune", external_id: "dev-1" }])).toEqual([1]);
  });

  it("patches only what a row allows and counts the locked rows", () => {
    const out = patchRows("assets_exposure", [hand, synced], [0, 1], { criticality: "low" });
    expect(out.rows[0].criticality).toBe("low");
    expect(out.rows[1].criticality).toBe("unknown");
    expect(out).toMatchObject({ changed: 1, locked: 1 });
    const owner = patchRows("assets_exposure", [hand, synced], [0, 1], { owner: "SOC" });
    expect(owner).toMatchObject({ changed: 2, locked: 0 });
  });

  it("never deletes a synced row", () => {
    const out = deleteRows([hand, synced], [0, 1]);
    expect(out.rows).toEqual([synced]);
    expect(out).toMatchObject({ changed: 1, locked: 1 });
  });

  it("refuses an empty name and a name a hand row already holds", () => {
    expect(nameProblem([hand], " ")).toBe("empty");
    expect(nameProblem([hand], "BUILD-01")).toBe("taken");
    expect(nameProblem([hand], "build-01", 0)).toBeUndefined();
    expect(nameProblem([synced], "MacBook")).toBeUndefined();
  });
});
