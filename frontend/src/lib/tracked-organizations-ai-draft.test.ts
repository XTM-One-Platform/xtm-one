import { describe, expect, it } from "vitest";

import type { TrackedDraftRow } from "@/hooks/api/useOrganizationsApi";
import { decideBody, failedBatches, isDecidable, linkedOpenCti, runEstimate, typedNames } from "./tracked-organizations-ai-draft";

function row(name: string, patch: Partial<TrackedDraftRow> = {}): TrackedDraftRow {
  return {
    name,
    relationship: "provider",
    criticality: "medium",
    sector: "",
    regions: [],
    website: "",
    confidence: "medium",
    basis: "",
    opencti_matches: [],
    opencti_organization_id: "",
    already_tracked_id: "",
    error: "",
    notes: [],
    status: "pending",
    ...patch,
  };
}

describe("tracked organizations AI draft rules", () => {
  it("reads distinct typed names, one per line, blank lines and duplicates dropped", () => {
    expect(typedNames(" Acme \n\nacme\nGlobex  Corp\r\nInitech")).toEqual(["Acme", "Globex Corp", "Initech"]);
  });

  it("states the run before it runs: 50 names at most, one request per 10", () => {
    expect(runEstimate(12)).toEqual({ drafted: 12, requests: 2, overLimit: false });
    expect(runEstimate(120)).toEqual({ drafted: 50, requests: 5, overLimit: true });
    expect(runEstimate(0)).toEqual({ drafted: 0, requests: 0, overLimit: false });
  });

  it("never lets a conflict or failed row be accepted", () => {
    expect(isDecidable(row("A"))).toBe(true);
    expect(isDecidable(row("B", { status: "conflict", already_tracked_id: "x" }))).toBe(false);
    expect(isDecidable(row("C", { status: "error", error: "quota" }))).toBe(false);
  });

  it("sends only the accepted pending rows, with the OpenCTI organization picked over the draft's", () => {
    const rows = [
      row("Acme", { opencti_organization_id: "draft-1" }),
      row("Globex", { opencti_matches: [{ id: "g1", name: "Globex" }, { id: "g2", name: "Globex Ltd" }] }),
      row("Initech"),
      row("Tracked", { status: "conflict", already_tracked_id: "t1" }),
    ];
    const body = decideBody(rows, new Set(["Acme", "Globex", "Tracked"]), { Globex: "g2" });
    expect(body).toEqual({ accept: ["Acme", "Globex"], opencti_ids: { Acme: "draft-1", Globex: "g2" } });
    expect(linkedOpenCti(rows[0], { Acme: "picked" })).toBe("picked");
  });

  it("groups failed rows by batch error so the other batches stay", () => {
    const rows = [row("A", { status: "error", error: "quota spent" }), row("B", { status: "error", error: "quota spent" }), row("C")];
    expect(failedBatches(rows)).toEqual([{ error: "quota spent", names: ["A", "B"] }]);
  });
});
