// An investigation step's evidence: a page a source read is a link only when
// its address is a web one (`lib/safe-url.ts`). The address comes from a
// third party (a search result, a fetched page's final URL).

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { HOSTILE_URLS } from "@/testing/hostile-urls";
import type { Investigation, InvestigationStep } from "@/types";
import { InvestigationBlock } from "./InvestigationBlock";

let run: Investigation;
vi.mock("@/hooks/api/useInvestigationApi", async () => {
  const { awaitingReport, REPORT_WAIT_MS } = await import("@/components/investigation/goal-state");
  return {
    awaitingReport,
    REPORT_WAIT_MS,
    useInvestigation: () => ({ data: run, isLoading: false, isError: false, stalled: false }),
    // The continuation panel under a finished run: nothing to offer here.
    useContinuationOffer: () => ({ data: undefined, isLoading: false }),
    useStartContinuation: () => ({ mutate: vi.fn(), isPending: false }),
  };
});
vi.mock("@/hooks/useAiDisabled", () => ({ useAiActionDisabled: () => false }));

function renderEvidence(href: string) {
  const step = {
    step_id: "step-1",
    position: 0,
    step_status: "completed",
    source_name: "Web search",
    source_pack_slug: null,
    query: null,
    error_type: null,
    status_detail: null,
    detail_code: null,
    detail_params: null,
    findings_count: 1,
    confidence: null,
    evidence: [{ kind: "url", label: "Vendor advisory", href }],
    answer: null,
    iteration: null,
    lead_from_step_id: null,
  } as unknown as InvestigationStep;
  run = {
    id: "run-1",
    status: "completed",
    subject_type: "domain",
    subject_value: "example.org",
    goal_plan: null,
    steps: [step],
    summary: { total: 1, by_status: { completed: 1 } },
    duration_seconds: null,
    completed_at: null,
    end_reason: null,
    end_reason_code: null,
    end_reason_params: null,
    iterations_used: 0,
    max_iterations: 3,
    leads_dropped: 0,
  } as unknown as Investigation;
  const result = renderWithProviders(<InvestigationBlock investigationId="run-1" />);
  fireEvent.click(screen.getByRole("button", { expanded: false }));
  return result;
}

const hrefs = (root: HTMLElement) => [...root.querySelectorAll("a")].map((a) => a.getAttribute("href"));

describe("InvestigationBlock step evidence", () => {
  it("links a web address", () => {
    const { container } = renderEvidence("https://vendor.example.com/advisory");
    expect(hrefs(container)).toEqual(["https://vendor.example.com/advisory"]);
  });

  it.each(HOSTILE_URLS)("names a page at %s without linking it", (_label, url) => {
    const { container } = renderEvidence(url);
    expect(hrefs(container)).toEqual([]);
    expect(screen.getByText("Vendor advisory").tagName).toBe("SPAN");
  });
});
