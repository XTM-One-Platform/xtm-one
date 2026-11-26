// #2523: an Unmatched signal names the software the gate tried, so a reader sees why nothing matched.

import { describe, expect, it } from "vitest";

import type { CtemDecisionRecord } from "@/hooks/api/useCtemSignalsApi";
import { decisionInputs } from "./DecisionReason";

const t = (id: string) => id;

function decision(over: Partial<CtemDecisionRecord>): CtemDecisionRecord {
  return {
    outcome: "to_validate",
    reason_code: "subject_unresolved",
    reconcile: { assets: [], lenses: ["world"], software_refs: ["Ivanti Connect Secure", "Fortinet FortiOS"] },
    ...over,
  } as CtemDecisionRecord;
}

describe("Software tried", () => {
  it("lists the software references of an unmatched decision", () => {
    const row = decisionInputs(decision({}), t).find((input) => input.key === "software_tried");
    expect(row?.label).toBe("Software tried");
    expect(row?.value).toBe("Ivanti Connect Secure, Fortinet FortiOS");
  });

  it("is left out once the subject resolved", () => {
    const rows = decisionInputs(decision({ outcome: "confirmed", reason_code: "in_scope" }), t);
    expect(rows.some((input) => input.key === "software_tried")).toBe(false);
  });

  it("is left out when nothing was tried", () => {
    const rows = decisionInputs(decision({ reconcile: { assets: [], software_refs: [] } }), t);
    expect(rows.some((input) => input.key === "software_tried")).toBe(false);
  });
});
