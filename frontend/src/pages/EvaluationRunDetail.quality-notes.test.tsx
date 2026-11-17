// Run detail: why entries are left out of Overall Quality, and why an entry's metric
// scored 0 or was left out.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";
import type { PersistedMetricResult } from "@/types";
import { CoverageReasons, QualityNotes } from "./EvaluationRunDetail";

describe("CoverageReasons", () => {
  it("lists each reason with its entry count, a code as its sentence", () => {
    renderWithProviders(
      <CoverageReasons leftOut={{ reference_empty: 2, "No answer shape declared.": 1 }} />,
    );
    expect(screen.getByText("2 entries: The expected answer is empty.")).toBeTruthy();
    expect(screen.getByText("1 entry: No answer shape declared.")).toBeTruthy();
  });
});

describe("QualityNotes", () => {
  const results: Record<string, PersistedMetricResult> = {
    "json.field_accuracy": {
      status: "scored",
      value: 0,
      detail: { reason: "prediction_not_well_formed", hint: "Start the extractor with Find JSON." },
    },
    "prose.token_prf": { status: "skipped", value: null, detail: { reason: "reference_empty" } },
    "json.schema_validity": { status: "scored", value: 100, detail: {} },
    "performance.latency.wall_clock": { status: "unavailable", value: null, detail: { reason: "x" } },
  };

  it("shows a zero's reason and hint and a left-out metric's reason, nothing else", () => {
    renderWithProviders(<QualityNotes metricResults={results} />);
    expect(screen.getByText(/scored 0: The answer is not well-formed\./)).toBeTruthy();
    expect(screen.getByText("Start the extractor with Find JSON.")).toBeTruthy();
    expect(screen.getByText("left out").closest("p")?.textContent).toMatch(/left out: The expected answer is empty\./);
    expect(screen.queryByText(/wall_clock|Wall/)).toBeNull();
    expect(screen.queryByText(/schema/i)).toBeNull();
  });

  it("explains a left-out metric to a keyboard user", async () => {
    const user = userEvent.setup();
    renderWithProviders(<QualityNotes metricResults={results} />);
    const target = screen.getByText("left out");
    // Reached with Tab alone, as a keyboard user reaches it.
    for (let presses = 0; presses < 20 && document.activeElement !== target; presses++) await user.tab();
    expect(document.activeElement).toBe(target);
    expect((await screen.findByRole("tooltip")).textContent).toContain(
      "so the entry does not count in the metric's value for the run",
    );
  });

  it("renders nothing when every quality metric scored", () => {
    renderWithProviders(
      <QualityNotes metricResults={{ "json.schema_validity": { status: "scored", value: 100, detail: {} } }} />,
    );
    expect(screen.queryByText("Quality notes")).toBeNull();
  });
});

describe("an entry whose agent run raised", () => {
  it("says the agent's failure scored 0 and ours left the entry out", () => {
    renderWithProviders(
      <QualityNotes
        metricResults={{
          "classification.micro_prf": {
            status: "scored",
            value: 0,
            detail: { reason: "agent_run_failed", numerator: 0, denominator: 2 },
          },
        }}
      />,
    );
    expect(screen.getByText(/scored 0: The agent's run failed, so it gave no answer\./)).toBeTruthy();

    renderWithProviders(<CoverageReasons leftOut={{ platform_error: 1 }} />);
    expect(screen.getByText("1 entry: The platform could not run the agent.")).toBeTruthy();
  });
});
