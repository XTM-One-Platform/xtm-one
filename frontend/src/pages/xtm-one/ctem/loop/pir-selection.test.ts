// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";
import { pirSelection, sourceKey, withExclusion, withPirSelection } from "./signal-sources";

const PIRS = [
  { pir_id: "p-threat", pir_type: "THREAT_LANDSCAPE" },
  { pir_id: "p-origin", pir_type: "THREAT_ORIGIN" },
];

describe("PIR selection per path (#2436)", () => {
  it("defaults to the PIRs of the path's OpenCTI type, else every enabled PIR", () => {
    expect(pirSelection("relevant_threat", PIRS, undefined)).toEqual({ pirIds: ["p-origin", "p-threat"], available: ["p-origin", "p-threat"], isDefault: true });
    // OpenCTI offers no vulnerability PIR: none available of that type, every enabled PIR listened to.
    expect(pirSelection("new_vulnerability", PIRS, undefined)).toEqual({ pirIds: ["p-origin", "p-threat"], available: [], isDefault: true });
  });

  it("takes the administrator's pick, drops a PIR no longer enabled, and an empty pick provisions nothing", () => {
    expect(pirSelection("relevant_threat", PIRS, { relevant_threat: ["p-threat", "p-gone"] }).pirIds).toEqual(["p-threat"]);
    expect(pirSelection("relevant_threat", PIRS, { relevant_threat: [] })).toMatchObject({ pirIds: [], isDefault: false });
  });

  it("writes the pick without losing the other signal source settings, and null drops back to the default", () => {
    const current = { excluded: { new_exposure: ["gti:pass"] }, included: { code_flaw: ["github:listener:secret_scanning"] } };
    const next = withPirSelection(current, "relevant_threat", ["p-threat"]);
    expect(next).toEqual({ ...current, pirs: { relevant_threat: ["p-threat"] } });
    expect(withPirSelection(next, "relevant_threat", null)).toEqual(current);
  });
});

describe("signal source settings (#2436)", () => {
  it("names a source by its key, its name included", () => {
    expect(sourceKey({ provider: "github", kind: "listener", name: "secret_scanning" })).toBe("github:listener:secret_scanning");
    expect(sourceKey({ provider: "aikido", kind: "listener" })).toBe("aikido:listener");
  });

  it("switches a source off by default through `included`, and keeps `pirs` on every write", () => {
    const current = { excluded: {}, pirs: { relevant_threat: ["p-threat"] } };
    const on = withExclusion(current, "code_flaw", "github:listener:secret_scanning", false, false);
    expect(on).toEqual({ excluded: {}, included: { code_flaw: ["github:listener:secret_scanning"] }, pirs: { relevant_threat: ["p-threat"] } });
    expect(withExclusion(on, "code_flaw", "github:listener:secret_scanning", true, false)).toEqual(current);
    // A source on by default still goes through `excluded`.
    expect(withExclusion(on, "new_exposure", "gti:pass", true)).toMatchObject({ excluded: { new_exposure: ["gti:pass"] }, included: on.included, pirs: on.pirs });
  });
});
