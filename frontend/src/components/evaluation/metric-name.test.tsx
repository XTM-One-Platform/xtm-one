// A metric shows one name and says what it measures wherever it appears.

import { describe, expect, it } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { EvaluationScoringCard } from "./evaluation-scoring-card";
import { MetricName } from "./metric-name";

describe("MetricName", () => {
  it("names a package metric and says what it measures", async () => {
    renderWithProviders(<MetricName metricKey="classification.samples_f1" />);
    const name = screen.getByText("Label F1, Averaged per Entry (Samples)");
    fireEvent.focus(name);
    expect((await screen.findByRole("tooltip")).textContent).toContain(
      "The F1 of each entry's labels (found, missed, extra), then averaged over entries.",
    );
  });

  it("names a metric by the name it takes on one entry, and says what it measures there", async () => {
    renderWithProviders(<MetricName metricKey="classification.micro_prf" entryLabel="Label F1" />);
    expect(screen.queryByText("Label F1, All Labels Pooled (Micro)")).toBeNull();
    fireEvent.focus(screen.getByText("Label F1"));
    expect((await screen.findByRole("tooltip")).textContent).toContain(
      "Expected labels found on this entry, extra ones penalised (F1).",
    );
  });

  it("falls back to the backend's label, then to the key, for a key it does not name", () => {
    renderWithProviders(
      <>
        <MetricName metricKey="json.from_a_newer_backend" fallback="Newer metric" />
        <MetricName metricKey="constructor" />
      </>,
    );
    expect(screen.getByText("Newer metric")).toBeTruthy();
    expect(screen.getByText("constructor")).toBeTruthy();
  });
});

describe("the Scoring card's family metrics", () => {
  it("lists the family's metrics by name, not by key", () => {
    renderWithProviders(
      <EvaluationScoringCard
        editing
        familyCatalog={[
          { id: "STIX_BUNDLE", label: "STIX Bundle", metric_keys: ["stix.entity_prf", "stix.envelope_validity"] },
        ]}
        family="STIX_BUNDLE"
        onFamilyChange={() => {}}
        judgeEnabled={false}
        onJudgeEnabledChange={() => {}}
        performanceTolerance={5}
        onPerformanceToleranceChange={() => {}}
        penaltyFactor={0.5}
        onPenaltyFactorChange={() => {}}
      />,
    );
    expect(screen.getByText("Entity F1")).toBeTruthy();
    expect(screen.getByText("Readable Bundle")).toBeTruthy();
    expect(screen.queryByText("stix.entity_prf")).toBeNull();
  });

  it("names them on the read-only card too", () => {
    renderWithProviders(
      <EvaluationScoringCard
        editing={false}
        familyCatalog={[
          { id: "CLASSIFICATION", label: "Classification", metric_keys: ["classification.samples_f1"] },
        ]}
        family="CLASSIFICATION"
        onFamilyChange={() => {}}
        judgeEnabled={false}
        onJudgeEnabledChange={() => {}}
        performanceTolerance={5}
        onPerformanceToleranceChange={() => {}}
        penaltyFactor={0.5}
        onPenaltyFactorChange={() => {}}
        evaluationSnapshot={{ family: "CLASSIFICATION", penalty_factor: 0.5, performance_tolerance: 5 }}
      />,
    );
    expect(screen.getByText("Label F1, Averaged per Entry (Samples)")).toBeTruthy();
    expect(screen.queryByText("classification.samples_f1")).toBeNull();
  });
});
