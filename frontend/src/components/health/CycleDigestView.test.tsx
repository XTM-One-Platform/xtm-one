// A Module Analyst cycle's digest in the Cycles tab (#101 S14d-2, dev-docs 12.7):
// the recommendations it attached by confidence, the agentic findings it opened
// and resolved, its proposals, and its spend named against its own ceiling;
// another specialist's digest keeps its Findings and Experiments figures.
// Asserted on roles, text and test ids (src/no-class-name-assertions.test.ts).

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen } from "@testing-library/react";

import { CycleDigestView } from "@/components/health/CycleDigestView";
import { renderWithProviders } from "@/testing/providers";
import type { HealthDigest } from "@/types";

vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<object>()),
  api: { get: vi.fn(() => Promise.resolve({ name: "Platform Health", default_name: "Platform Health" })), put: vi.fn(), post: vi.fn() },
}));

const base: HealthDigest = {
  cycle_id: "c1",
  specialist: "module_analyst",
  trigger: "signal",
  target_type: "module",
  target_id: "r1",
  status: "completed",
  opened_at: "2026-11-21T10:00:00Z",
  closed_at: "2026-11-21T10:01:00Z",
  run_id: "run1",
  run_status: "completed",
  stop_reason: null,
  signal: null,
  consolidations: null,
  findings: null,
  experiments: null,
  proposals: { filed: 1, applied: 0, rolled_back: 0, by_verdict: {} },
  budget: { spent_tokens: 22600, spent_cost_usd: "0.108", cap_tokens: 80000, cap_cost_usd: null, ledger: "module_analyst" },
  dry_run_writes: null,
  recommendations: { attached: 3, high: 2, medium: 1, low: 0 },
  agentic_findings: { opened: 1, resolved: 0 },
  skipped: [],
  unavailable: [],
  narrative: null,
};

afterEach(cleanup);

describe("CycleDigestView", () => {
  it("shows a Module Analyst cycle's recommendations, agentic findings and its own ceiling", () => {
    renderWithProviders(<CycleDigestView digest={base} />);
    expect(screen.getByText("Recommendations attached")).toBeInTheDocument();
    expect(screen.getByText("3 · high confidence 2 · medium confidence 1")).toBeInTheDocument();
    expect(screen.getByText("Agentic findings")).toBeInTheDocument();
    expect(screen.getByText("opened 1")).toBeInTheDocument();
    expect(screen.getByText("Proposals")).toBeInTheDocument();
    expect(screen.getByText("Spent on the Module Analyst ceiling")).toBeInTheDocument();
    expect(screen.queryByText("Experiments")).toBeNull();
    expect(screen.queryByText("Budget spent")).toBeNull();
  });

  it.each(["opencti_checker", "openaev_checker", "opencrq_checker"])(
    "shows the module figures of a %s cycle (#101 S15)",
    (specialist) => {
      renderWithProviders(<CycleDigestView digest={{ ...base, specialist }} />);
      expect(screen.getByText("Recommendations attached")).toBeInTheDocument();
      expect(screen.getByText("Agentic findings")).toBeInTheDocument();
      expect(screen.queryByText("Findings")).toBeNull();
      expect(screen.queryByText("Experiments")).toBeNull();
    },
  );

  it("writes 0 when the cycle attached nothing", () => {
    renderWithProviders(
      <CycleDigestView digest={{ ...base, recommendations: { attached: 0, high: 0, medium: 0, low: 0 }, agentic_findings: { opened: 0, resolved: 0 } }} />,
    );
    expect(screen.getAllByText("0")).toHaveLength(2);
  });

  describe("the weekly digest's Modules part (#101 S15e)", () => {
    const weekly: HealthDigest = {
      ...base,
      specialist: "orchestrator",
      target_type: null,
      target_id: null,
      recommendations: undefined,
      agentic_findings: undefined,
      budget: { ...base.budget, ledger: undefined },
    };

    it("lists each module's rounds, verdict totals and the checks failed most", () => {
      renderWithProviders(
        <CycleDigestView
          digest={{
            ...weekly,
            modules: {
              opencti: {
                rounds: 3,
                registrations: 2,
                passed: 4,
                warned: 2,
                failed: 2,
                not_run: 1,
                most_failed: [{ id: "managers_running", failed: 2, warned: 0 }],
              },
              opencrq: { rounds: 1, registrations: 1, passed: 1, warned: 0, failed: 0, not_run: 0, most_failed: [] },
            },
          }}
        />,
      );
      expect(screen.getByRole("heading", { name: "Module checks" })).toBeInTheDocument();
      const cti = screen.getByTestId("health-digest-module-opencti");
      expect(cti).toHaveTextContent("OpenCTI");
      expect(cti).toHaveTextContent("3 rounds on 2 registrations");
      expect(cti).toHaveTextContent("4 passed, 2 warnings, 2 failed, 1 not run");
      expect(cti).toHaveTextContent("Failed most");
      expect(cti).toHaveTextContent("(2 failed, 0 warnings)");
      const crq = screen.getByTestId("health-digest-module-opencrq");
      expect(crq).toHaveTextContent("1 round on 1 registration");
      expect(crq).toHaveTextContent("1 passed, 0 warnings, 0 failed");
      expect(crq).not.toHaveTextContent("not run");
      expect(crq).not.toHaveTextContent("Failed most");
    });

    it("says when no round ran since the last digest", () => {
      renderWithProviders(<CycleDigestView digest={{ ...weekly, modules: {} }} />);
      expect(screen.getByTestId("health-digest-modules")).toHaveTextContent("No check round since the last digest");
    });

    it("says Unavailable, never 0, when the reports could not be read", () => {
      renderWithProviders(<CycleDigestView digest={{ ...weekly, modules: null, unavailable: ["modules"] }} />);
      expect(screen.getByTestId("health-digest-modules")).toHaveTextContent("Unavailable");
    });

    it("is absent from another cycle's digest", () => {
      renderWithProviders(<CycleDigestView digest={base} />);
      expect(screen.queryByTestId("health-digest-modules")).toBeNull();
    });
  });

  it("keeps the Findings and Experiments figures of another specialist", () => {
    renderWithProviders(
      <CycleDigestView
        digest={{ ...base, specialist: "quality_analyst", target_type: "agent", recommendations: undefined, agentic_findings: undefined, budget: { ...base.budget, ledger: undefined } }}
      />,
    );
    expect(screen.getByText("Findings")).toBeInTheDocument();
    expect(screen.getByText("Experiments")).toBeInTheDocument();
    expect(screen.getByText("Budget spent")).toBeInTheDocument();
    expect(screen.queryByText("Recommendations attached")).toBeNull();
  });
});
