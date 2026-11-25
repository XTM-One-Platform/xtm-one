// "How the Exposure Posture is computed" (#11689): both readings per stage
// and both totals, straight from the payload, never recomputed.

import { describe, expect, it } from "vitest";
import { screen, within } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { CtemPosture, CtemStage } from "@/types";
import { PostureExplainDialog } from "./PostureExplainDialog";

function stage(key: string, over: Partial<CtemStage> = {}): CtemStage {
  return {
    key, label: key, score: 64, prev_score: null, band: "fair", covered: true, confidence: "high",
    primary_products: [], secondary_products: [], missing_products: [], products_present: [],
    methodology: "", facts: [], viz: {}, qualitative_score: 52, qualitative_band: "at_risk",
    ...over,
  };
}

function posture(over: Partial<CtemPosture> = {}): CtemPosture {
  return {
    posture_score: 40, posture_band: "at_risk", covered_stages: 5, total_stages: 5, vertical: "cyber",
    weights: { scoping: 0.15, discovery: 0.25, prioritization: 0.25, validation: 0.2, remediation: 0.15 },
    stages: [
      stage("scoping", { score: 100, band: "strong", qualitative_score: 76, qualitative_band: "good" }),
      stage("discovery", { score: 3, band: "critical", qualitative_score: 2, qualitative_band: "critical" }),
      stage("prioritization", { score: 89, band: "good", qualitative_score: 70, qualitative_band: "fair" }),
      stage("validation", { score: 4, band: "critical", qualitative_score: null, qualitative_band: "unknown" }),
      stage("remediation", { score: 9, band: "critical", qualitative_score: 5, qualitative_band: "critical" }),
    ],
    products: {}, trend: [], blind_spots: [], headline: {} as CtemPosture["headline"], last_sync: {},
    qualitative_score: 33, qualitative_band: "critical", context: { state: "partial", missing_fields: ["software"] },
    ...over,
  };
}

describe("PostureExplainDialog", () => {
  it("lists every stage with its weight, its measured score and its score in your context", () => {
    renderWithProviders(<PostureExplainDialog open onOpenChange={() => undefined} posture={posture()} />);
    const scoping = screen.getByTestId("ctem-posture-explain-scoping");
    expect(within(scoping).getAllByRole("cell").map((c) => c.textContent)).toEqual(["15%", "100", "76"]);
    const validation = screen.getByTestId("ctem-posture-explain-validation");
    expect(within(validation).getAllByRole("cell").map((c) => c.textContent)).toEqual(["20%", "4", "--"]);
  });

  it("shows both totals: the measured posture and the posture in your context", () => {
    renderWithProviders(<PostureExplainDialog open onOpenChange={() => undefined} posture={posture()} />);
    const total = screen.getByTestId("ctem-posture-explain-total");
    expect(within(total).getAllByRole("cell").map((c) => c.textContent)).toEqual(["", "40", "33"]);
    expect(screen.getByRole("columnheader", { name: "Measured" })).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "In your context" })).toBeTruthy();
    expect(screen.queryByTestId("ctem-posture-explain-no-context")).toBeNull();
  });

  it("asks for the organization context when the posture has no context reading", () => {
    renderWithProviders(
      <PostureExplainDialog open onOpenChange={() => undefined} posture={posture({ qualitative_score: null, qualitative_band: "unknown", context: { state: "missing", missing_fields: [] } })} />,
    );
    const total = screen.getByTestId("ctem-posture-explain-total");
    expect(within(total).getAllByRole("cell")[2]?.textContent).toBe("--");
    expect(screen.getByTestId("ctem-posture-explain-no-context")).toBeTruthy();
  });
});
