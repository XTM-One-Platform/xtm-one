// #2523: an Unmatched signal names the software the gate tried, so a reader sees why nothing matched.

import { describe, expect, it } from "vitest";

import type { CtemDecisionRecord } from "@/hooks/api/useCtemSignalsApi";
import { decisionInputs, listSummary } from "./DecisionReason";

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

  // UX r1 B1 on #2533: a long list reads the count, the first 3 names and "+N more", every name kept for the tooltip.
  it("draws a list of more than 3 names as a counted summary of software", () => {
    const names = ["Ivanti Connect Secure", "Fortinet FortiOS", "Citrix NetScaler", "F5 BIG-IP", "Apache Struts", "Oracle WebLogic Server"];
    const row = decisionInputs(decision({ reconcile: { assets: [], software_refs: names } }), t).find((input) => input.key === "software_tried");
    expect(row?.names).toEqual(names);
    expect(row?.noun).toBe("software");
    const tr = (id: string, values?: Record<string, unknown>) => (id.startsWith("{total}") ? `${values?.total} software names: ${values?.names}` : `+${values?.count} more`);
    expect(listSummary(row?.names ?? [], "en", tr as never, false, "software")).toBe(
      "6 software names: Ivanti Connect Secure, Fortinet FortiOS, Citrix NetScaler, +3 more",
    );
  });

  // UX r1 B2 on #2533: a lookup that never read OpenCTI says so, never an absent row.
  it.each([
    ["no OpenCTI access", "Not read: no OpenCTI access"],
    ["OpenCTI did not answer", "Not read: OpenCTI did not answer"],
  ])("shows %s as a named value when nothing was tried", (lookup, expected) => {
    const rows = decisionInputs(decision({ reconcile: { assets: [], software_refs: [], threat_lookup: lookup } }), t);
    expect(rows.find((input) => input.key === "software_tried")?.value).toBe(expected);
  });

  it("reads cve_lookup the same way", () => {
    const rows = decisionInputs(decision({ reconcile: { assets: [], cve_lookup: "no OpenCTI access" } }), t);
    expect(rows.find((input) => input.key === "software_tried")?.value).toBe("Not read: no OpenCTI access");
  });

  it("names the missed read next to the names tried from the evidence", () => {
    const rows = decisionInputs(decision({ reconcile: { assets: [], software_refs: ["Zimbra"], threat_lookup: "OpenCTI did not answer" } }), t);
    expect(rows.find((input) => input.key === "software_tried")?.value).toBe("Zimbra");
    expect(rows.find((input) => input.key === "software_not_read")?.value).toBe("Not read: OpenCTI did not answer");
  });

  it("stays quiet when the read went through or the subject resolved", () => {
    const read = decisionInputs(decision({ reconcile: { assets: [], software_refs: [], threat_lookup: "read" } }), t);
    expect(read.some((input) => input.key === "software_tried" || input.key === "software_not_read")).toBe(false);
    const resolved = decisionInputs(decision({ outcome: "confirmed", reason_code: "in_scope", reconcile: { assets: [], threat_lookup: "no OpenCTI access" } }), t);
    expect(resolved.some((input) => input.key === "software_tried")).toBe(false);
  });
});
