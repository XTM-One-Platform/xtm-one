import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import type { RoiReportResponse } from "@/hooks/api/useRoiReportApi";
import type { ValueRemainder, ValueRow } from "@/lib/roi-value";
import { RemainderLabel, ValueCostNote } from "./Value";

const remainder: ValueRemainder = {
  key: "other",
  label: "Other agents and fleets",
  hint: "Agents and fleets you cannot open, summed.",
  row: {} as ValueRow,
};

describe("RemainderLabel", () => {
  it("truncates on one line with the full label in a tooltip, like the row name cell (UX round 2 on #10020, B1)", () => {
    renderWithProviders(<RemainderLabel remainder={remainder} />);
    const text = screen.getByText("Other agents and fleets", { selector: "[data-truncate]" });
    expect(text.getAttribute("data-truncate")).toBe("true");
    expect(text.getAttribute("title")).toBe("Other agents and fleets");
  });
});

function report(overrides: Partial<RoiReportResponse>): RoiReportResponse {
  return {
    window: "30d",
    currency: "USD",
    rate_label: "assumption",
    estimated: false,
    organization: {} as RoiReportResponse["organization"],
    agents: [],
    fleets: [],
    cost_source: "llm_analytics_by_agent",
    unallocated_cost: 0,
    ...overrides,
  };
}

describe("ValueCostNote (#10441)", () => {
  it("names LLM Analytics as the cost source and the part no unit carries", () => {
    renderWithProviders(<ValueCostNote report={report({ unallocated_cost: 12.5 })} />);
    const note = screen.getByTestId("value-cost-note");
    expect(note.textContent).toContain("the same figure as LLM Analytics");
    expect(note.textContent).toContain("$12.50 of it is not tied to a unit of work");
  });

  it("leaves the remainder sentence out when every cost is tied to a unit", () => {
    renderWithProviders(<ValueCostNote report={report({ unallocated_cost: 0 })} />);
    expect(screen.getByTestId("value-cost-note").textContent).not.toContain("not tied to a unit of work");
  });

  it("draws nothing when costs are hidden or the report reads another source", () => {
    const { rerender } = renderWithProviders(<ValueCostNote report={report({ cost_masked: true, unallocated_cost: null })} />);
    expect(screen.queryByTestId("value-cost-note")).toBeNull();
    rerender(<ValueCostNote report={report({ cost_source: "recorded_runs" })} />);
    expect(screen.queryByTestId("value-cost-note")).toBeNull();
  });
});
