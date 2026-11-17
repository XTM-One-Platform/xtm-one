import { describe, expect, it } from "vitest";

import type { AiDraftProposal, OrganizationProfile } from "@/hooks/api/useOrganizationContextApi";
import {
  acceptAll,
  acceptedSources,
  applyAccepted,
  formatProposalAmount,
  formatProposalValue,
  isBulkAcceptable,
  parseProposalAmount,
} from "./organization-ai-draft";

const row = (over: Partial<AiDraftProposal>): AiDraftProposal => ({
  field: "size",
  value: "251-1000",
  source: "description",
  confidence: "high",
  basis: "",
  check: false,
  current: "",
  conflict: false,
  ...over,
});

const PROFILE = {
  name: "Acme",
  sector: "Finance",
  size: "",
  risk_appetite: { annual_loss_threshold: null, approval_threshold: null },
} as unknown as OrganizationProfile;

describe("organization AI draft review rules (#9484)", () => {
  it("never accepts a conflict, a Check this or a low confidence row in bulk", () => {
    expect(isBulkAcceptable(row({}))).toBe(true);
    expect(isBulkAcceptable(row({ conflict: true }))).toBe(false);
    expect(isBulkAcceptable(row({ check: true }))).toBe(false);
    expect(isBulkAcceptable(row({ confidence: "low" }))).toBe(false);
  });

  it("counts the rows Accept all left to review", () => {
    const rows = [row({ field: "size" }), row({ field: "sector", conflict: true }), row({ field: "currency", confidence: "low" })];
    const { accepted, skipped } = acceptAll(rows, new Set());
    expect([...accepted]).toEqual(["size"]);
    expect(skipped).toBe(2);
    // A row the administrator already accepted by hand stays accepted and is not counted as skipped.
    expect(acceptAll(rows, new Set(["sector"])).skipped).toBe(1);
  });

  it("writes only the accepted rows, a nested path and an edited value included", () => {
    const rows = [
      row({ field: "size", value: "1001-5000" }),
      row({ field: "sector", value: "Insurance", conflict: true, current: "Finance" }),
      row({ field: "risk_appetite.approval_threshold", value: 50000, check: true }),
    ];
    const merged = applyAccepted(PROFILE, rows, new Set(["size", "risk_appetite.approval_threshold"]), { "risk_appetite.approval_threshold": 40000 });
    expect(merged.size).toBe("1001-5000");
    expect(merged.sector).toBe("Finance");
    expect(merged.risk_appetite).toEqual({ annual_loss_threshold: null, approval_threshold: 40000 });
    // The input profile is never mutated.
    expect(PROFILE.size).toBe("");
  });

  it("keeps the provenance of each accepted row and names the editor of an edited value", () => {
    const rows = [row({ field: "size", basis: "4,000 people" }), row({ field: "currency", value: "EUR", basis: "France" })];
    expect(acceptedSources(rows, new Set(["size", "currency"]), { currency: "USD" }, "admin@example.com")).toEqual({
      size: { source: "description", basis: "4,000 people" },
      currency: { source: "description", basis: "edited by admin@example.com" },
    });
  });

  it("formats list and entry values on one line", () => {
    expect(formatProposalValue(["FR", "DE"])).toBe("FR, DE");
    expect(formatProposalValue([{ product: "Salesforce", vendor: "Salesforce Inc", cpe: "" }])).toBe("Salesforce (Salesforce Inc)");
    expect(formatProposalValue(null)).toBe("");
  });

  it("reads a typed amount: digits only, grouping spaces ignored, anything else invalid (UX round 1, B2)", () => {
    expect(parseProposalAmount("2000000")).toBe(2000000);
    expect(parseProposalAmount("2 000 000")).toBe(2000000);
    expect(parseProposalAmount("2\u202f000")).toBe(2000);
    expect(parseProposalAmount("")).toBeNull();
    expect(parseProposalAmount("2 M EUR")).toBeUndefined();
    expect(parseProposalAmount("-5")).toBeUndefined();
  });

  it("shows an amount grouped and with its currency", () => {
    expect(formatProposalAmount(2000000, "EUR", "en-US")).toBe("\u20ac2,000,000.00");
    expect(formatProposalAmount(2000000, "", "en-US")).toBe("2,000,000");
    expect(formatProposalAmount(null, "EUR", "en-US")).toBe("");
  });
});
