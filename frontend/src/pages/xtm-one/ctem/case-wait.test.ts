/**
 * Who a case Waiting for decision waits on, never the raw `by` (UX r1 B3 on #1515) - `yarn test`.
 */
import { describe, expect, it } from "vitest";
import { caseWaitWho, caseWaitWhoLabel } from "./case-wait";

const ADA = "6f1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f";
const GHOST = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
const names = (id: string | null) => (id === ADA ? "Ada Lovelace" : null);

describe("caseWaitWho", () => {
  it("names a user the directory resolves", () => {
    expect(caseWaitWho(ADA, names)).toEqual({ kind: "name", name: "Ada Lovelace" });
  });
  it("never prints a user id the directory does not return", () => {
    expect(caseWaitWho(GHOST, names)).toEqual({ kind: "person" });
  });
  it("never prints a role, a policy or the loop's reason text", () => {
    expect(caseWaitWho("ctem.mobilization_role", names)).toEqual({ kind: "person" });
    expect(caseWaitWho("Above the approval threshold (EUR 50k)", names)).toEqual({ kind: "person" });
  });
  it("is a decision when nobody is named", () => {
    expect(caseWaitWho(null, names)).toEqual({ kind: "decision" });
    expect(caseWaitWho("  ", names)).toEqual({ kind: "decision" });
  });
  it("labels the fallbacks through the translator", () => {
    const t = (key: string) => `[${key}]`;
    expect(caseWaitWhoLabel({ kind: "person" }, t)).toBe("[A person]");
    expect(caseWaitWhoLabel({ kind: "decision" }, t)).toBe("[A decision]");
    expect(caseWaitWhoLabel({ kind: "name", name: "Ada" }, t)).toBe("Ada");
  });
});
