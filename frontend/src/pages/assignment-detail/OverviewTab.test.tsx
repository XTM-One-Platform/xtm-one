// The Recent health tile holds two segments at most: a
// third one ("3 completed, 2 failed, 2 in progress") overflowed the value's
// 2-line clamp at 1024. In progress is the value only when it is the only
// status; otherwise it moves to the subtitle.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { Assignment, AssignmentRun } from "@/types";

let RUNS: Partial<AssignmentRun>[] = [];

vi.mock("@/hooks/api/useAssignmentApi", () => ({
  useAssignmentRuns: () => ({ data: RUNS }),
}));
vi.mock("@/hooks/api/useIntegrationApi", () => ({
  useProviderCatalog: () => ({ data: [] }),
  useIntegrations: () => ({ data: [] }),
}));
vi.mock("@/hooks/api/useKnowledgeBaseApi", () => ({
  useKnowledgeBases: () => ({ data: [] }),
}));
vi.mock("./TriggerCard", () => ({ TriggerCard: () => null }));
vi.mock("./ParametersCard", () => ({ ParametersCard: () => null }));
vi.mock("./RunAsCard", () => ({ RunAsCard: () => null }));
vi.mock("./AssignmentStatus", () => ({ AssignmentStatusTile: () => null }));

const { OverviewTab } = await import("./OverviewTab");

const runs = (statuses: string[]) => statuses.map((status, i) => ({ id: `r${i}`, status }));

const assignment = { id: "a1", execution_count: 7, last_triggered_at: null } as unknown as Assignment;

function health() {
  // Total runs is the first tile; the status tile is mocked out; Recent health is the last.
  const values = screen.getAllByTestId("stat-card-value");
  const value = values[values.length - 1];
  const subtitle = value.nextElementSibling as HTMLElement;
  return { value: value.textContent ?? "", subtitle: subtitle.textContent ?? "" };
}

describe("assignment Overview Recent health tile", () => {
  it("keeps in progress out of the value when completed and failed runs are there, and says it in the subtitle", () => {
    RUNS = runs(["completed", "completed", "failed", "running"]);
    renderWithProviders(<OverviewTab assignment={assignment} agent={undefined} canManage={false} onUpdate={() => {}} />);
    const { value, subtitle } = health();
    expect(value).toBe("2 completed, 1 failed");
    expect(value).not.toContain("in progress");
    expect(subtitle).toBe("1 in progress, from the last 4 runs");
  });

  it("shows in progress as the value when it is the only status", () => {
    RUNS = runs(["pending"]);
    renderWithProviders(<OverviewTab assignment={assignment} agent={undefined} canManage={false} onUpdate={() => {}} />);
    const { value, subtitle } = health();
    expect(value).toBe("1 in progress");
    expect(subtitle).toBe("from the last run");
  });

  it("keeps the plain subtitle when no run is in progress", () => {
    RUNS = runs(["completed", "failed", "failed"]);
    renderWithProviders(<OverviewTab assignment={assignment} agent={undefined} canManage={false} onUpdate={() => {}} />);
    const { value, subtitle } = health();
    expect(value).toBe("1 completed, 2 failed");
    expect(subtitle).toBe("from the last 3 runs");
  });

  // At 0 the value is the placeholder dash and "no recent runs" sits on the
  // wrapping subtitle line (the sentence was clamped in fr at text-2xl).
  it("shows the placeholder value and no recent runs in the subtitle at 0", () => {
    RUNS = [];
    renderWithProviders(<OverviewTab assignment={assignment} agent={undefined} canManage={false} onUpdate={() => {}} />);
    const { value, subtitle } = health();
    expect(value).toBe("\u2014");
    expect(subtitle).toBe("no recent runs");
  });
});
