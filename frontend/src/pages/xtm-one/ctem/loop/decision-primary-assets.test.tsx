// #1591 S3 PR B: the trace names the primary assets reached with their chain,
// and the one scope threshold with where it comes from.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import type { CtemDecisionRecord } from "@/hooks/api/useCtemSignalsApi";
import { renderWithProviders } from "@/testing/providers";
import { DecisionReason } from "./DecisionReason";
import { primaryAssetsRow, thresholdWithSource } from "./decision-primary-assets";

const t = (id: string, values?: Record<string, string | number>) =>
  Object.entries(values ?? {}).reduce((s, [k, v]) => s.replace(`{${k}}`, String(v)), id);

const hit = {
  primary_asset_key: "payroll",
  software: "openssl 3.0.1",
  asset: "LAPTOP-042",
  through: "Finance laptops",
  through_kind: "asset_group",
  source: "opencrq",
  roles: ["supporting"],
  via: "",
};

const inScope: CtemDecisionRecord = {
  outcome: "case",
  reason_code: "in_scope",
  reason_params: { criticality: "critical", threshold: "high", confidence: "high" },
  reconcile: { assets: ["LAPTOP-042"], primary_assets: [hit], inventory_links_read: true },
  scope: Object.assign({ threshold_source: "policy" }, {
    criticality: "critical",
    threshold: "high",
    sources: [Object.assign({ from: "crown_jewel", name: "Payroll", criticality: "critical" }, { chain: [hit] })],
    risk_scenarios: [],
  }),
};

describe("primary assets reached", () => {
  it("writes the chain software, asset, group, primary asset and role, under the source's name", () => {
    expect(primaryAssetsRow(inScope, t)).toBe("openssl 3.0.1 → LAPTOP-042 → group Finance laptops → Payroll as supporting");
    const direct: CtemDecisionRecord = {
      reconcile: { primary_assets: [{ primary_asset_key: "payroll", asset: "srv-1", through_kind: "asset" }], inventory_links_read: true },
    };
    expect(primaryAssetsRow(direct, t)).toBe("srv-1 → payroll");
  });

  it("keeps a hit no criticality source names, by its key", () => {
    const record: CtemDecisionRecord = { reconcile: { primary_assets: [hit], inventory_links_read: true }, scope: null };
    expect(primaryAssetsRow(record, t)).toBe("openssl 3.0.1 → LAPTOP-042 → group Finance laptops → payroll as supporting");
  });

  it("says none was reached once the links were read, nothing when they were not", () => {
    const removed: CtemDecisionRecord = { reconcile: { primary_assets: [], inventory_links_read: true } };
    expect(primaryAssetsRow(removed, t)).toBe("None: no inventory link reaches a primary asset");
    expect(primaryAssetsRow({ reconcile: {} }, t)).toBe("");
    expect(primaryAssetsRow(null, t)).toBe("");
  });
});

describe("the one scope threshold", () => {
  it("names where it comes from", () => {
    expect(thresholdWithSource("High", "policy", t)).toBe("High, set by the prioritization policy");
    expect(thresholdWithSource("High", "default", t)).toBe("High, scope default");
    expect(thresholdWithSource("High", undefined, t)).toBe("High");
    expect(thresholdWithSource("", "policy", t)).toBe("");
  });

  it("draws the chain and the threshold with its source in the trace inputs", () => {
    renderWithProviders(<DecisionReason state="case_opened" decision={inScope} showInputs />);
    const inputs = screen.getByTestId("ctem-decision-inputs");
    expect(inputs).toHaveTextContent("Primary assets reached");
    expect(inputs).toHaveTextContent("openssl 3.0.1 → LAPTOP-042 → group Finance laptops → Payroll as supporting");
    expect(inputs).toHaveTextContent("High, set by the prioritization policy");
  });
});
