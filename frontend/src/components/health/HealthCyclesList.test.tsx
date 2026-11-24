// The Cycles tab names each cycle's target after its specialist (#101 S15e, dev-docs 13.8):
// a module checker's round reads the checker then the registration's title, and a cycle whose
// target no longer exists shows no name. Asserted on text and test ids
// (src/no-class-name-assertions.test.ts).

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen } from "@testing-library/react";

import { HealthCyclesList } from "@/components/health/HealthCyclesList";
import { renderWithProviders } from "@/testing/providers";
import type { HealthCycle } from "@/types";

const cycles: HealthCycle[] = [];

vi.mock("@/hooks/api/useHealthApi", () => ({
  useHealthCycles: () => ({ data: cycles, isLoading: false, isError: false }),
  useHealthName: () => "Platform Health",
}));

function cycle(overrides: Partial<HealthCycle>): HealthCycle {
  return {
    id: "c1",
    specialist: "opencti_checker",
    trigger: "schedule",
    target_type: "module",
    target_id: "r1",
    status: "completed",
    stop_reason: null,
    opened_at: "2026-11-22T10:00:00Z",
    closed_at: "2026-11-22T10:01:00Z",
    spent_tokens: 0,
    spent_cost_usd: "0",
    run_id: null,
    signal: null,
    digest: null,
    requested_by_me: false,
    target_name: null,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  cycles.length = 0;
});

describe("HealthCyclesList", () => {
  it("names a module checker's round by the checker and the registration's title", () => {
    cycles.push(cycle({ target_name: "OpenCTI Europe" }));
    renderWithProviders(<HealthCyclesList />);
    expect(screen.getByText("OpenCTI Health Checker")).toBeInTheDocument();
    expect(screen.getByTestId("health-cycle-target")).toHaveTextContent("OpenCTI Europe");
  });

  it("shows no target name when the target no longer exists", () => {
    cycles.push(cycle({ id: "c2", specialist: "openaev_checker", target_name: null }));
    renderWithProviders(<HealthCyclesList />);
    expect(screen.getByText("OpenAEV Health Checker")).toBeInTheDocument();
    expect(screen.queryByTestId("health-cycle-target")).toBeNull();
  });
});
