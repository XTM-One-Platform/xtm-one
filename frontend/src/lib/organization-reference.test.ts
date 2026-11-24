// SPDX-License-Identifier: Apache-2.0

// The reference rates, PIR details and field provenance of the organization profile (#1773).
import { describe, expect, it } from "vitest";
import {
  emptyRateRow,
  missingRateInto,
  pirDetailProblem,
  pirDetailsOf,
  provenanceOf,
  rateProblem,
  ratesOf,
} from "./organization-reference";

const ROW = { from_currency: "usd", to_currency: "EUR", rate: "0,92", as_of: "2026-11-24", source: "ECB" };

describe("reference exchange rates (#1773)", () => {
  it("saves a valid row upper-cased, with a decimal comma read as a point", () => {
    expect(rateProblem(ROW)).toBeNull();
    expect(ratesOf([ROW])).toEqual([{ from_currency: "USD", to_currency: "EUR", rate: 0.92, as_of: "2026-11-24", source: "ECB" }]);
  });

  it("names the first problem of a row and refuses the save", () => {
    expect(rateProblem({ ...ROW, from_currency: "US" })).toBe("currency");
    expect(rateProblem({ ...ROW, from_currency: "EUR" })).toBe("same_currency");
    expect(rateProblem({ ...ROW, rate: "0" })).toBe("rate");
    expect(rateProblem({ ...ROW, rate: "abc" })).toBe("rate");
    expect(rateProblem({ ...ROW, as_of: "24/11/2026" })).toBe("date");
    expect(rateProblem({ ...ROW, source: " " })).toBe("source");
    expect(ratesOf([ROW, emptyRateRow("EUR")])).toBeNull();
  });

  it("says when no rate converts into the profile currency", () => {
    expect(missingRateInto("EUR", [])).toBe(true);
    expect(missingRateInto("EUR", ratesOf([ROW]) ?? [])).toBe(false);
    expect(missingRateInto("", [])).toBe(false);
  });
});

describe("PIR details (#1773)", () => {
  it("keeps the details of followed PIRs that say something, trimmed and each jewel once", () => {
    const details = {
      a: { question: " Who targets us? ", owner: "CTI", review_cadence: "monthly" as const, primary_assets: ["Pay", "Pay", " "] },
      b: { question: "", owner: "", review_cadence: "" as const, primary_assets: [] },
      gone: { question: "Old", owner: "", review_cadence: "" as const, primary_assets: [] },
    };
    expect(pirDetailsOf(["a", "b"], details)).toEqual({
      a: { question: "Who targets us?", owner: "CTI", review_cadence: "monthly", primary_assets: ["Pay"] },
    });
  });

  it("bounds the question as the server does", () => {
    expect(pirDetailProblem({ question: "x".repeat(1001), owner: "", review_cadence: "", primary_assets: [] })).not.toBeNull();
    expect(pirDetailProblem({ question: "x", owner: "", review_cadence: "", primary_assets: [] })).toBeNull();
  });
});

describe("field provenance (#1773)", () => {
  it("reads a field's source, basis and confirmation", () => {
    const sources = { fx_rates: { source: "agent:abc", basis: "ECB page", confirmed_at: "2026-11-24T10:00:00Z" } };
    expect(provenanceOf(sources, "fx_rates")).toEqual({ source: "agent:abc", basis: "ECB page", confirmed_at: "2026-11-24T10:00:00Z" });
    expect(provenanceOf(sources, "pir_details")).toBeNull();
    expect(provenanceOf(null, "fx_rates")).toBeNull();
  });
});
