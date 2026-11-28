// The analyst review of a case (CTEM-16-UI, #1115): the threat package the
// fleet drafted in OpenCTI with its link, the coverage request, gate
// `ctem.analyst_review` skipped by policy (never hidden), a waiting review on
// the unified work-item decision card (never a CTEM card), who answered (a
// person or the analyst's mark), and a case with no package says so.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { CtemCaseEvent, CtemCaseThreatPackage } from "@/hooks/api/useCtemCasesApi";
import { reviewAnswerer, reviewDecisionKey, reviewStage } from "./case-review";

const state = vi.hoisted(() => ({ items: [] as unknown[], calls: [] as unknown[] }));

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: false }) }));
vi.mock("@/hooks/api/useWorkItemApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useWorkItemApi")>()),
  useWorkItems: (filters: unknown, enabled: boolean) => {
    state.calls.push({ filters, enabled });
    return { data: enabled ? state.items : undefined, isLoading: false, refetch: vi.fn() };
  },
}));
vi.mock("@/components/work-items/WorkItemDecisionCard", () => ({
  WorkItemDecisionCard: ({ inCase }: { inCase?: boolean }) => <div data-testid="unified-decision-card" data-in-case={String(Boolean(inCase))} />,
}));

const { CaseAnalystReview } = await import("./CaseAnalystReview");

const USER = "0b8f2a64-1c3d-4e5f-8a9b-0c1d2e3f4a5b";
const GROUPING = "8a3e2f10-4c5d-4b6e-9f70-1a2b3c4d5e6f";
const PKG: CtemCaseThreatPackage = {
  restricted: false,
  coverage_requested: true,
  grouping_id: GROUPING,
  name: "CVE-2026-1234 on nginx",
  url: `https://cti.example.test/dashboard/analyses/groupings/${GROUPING}`,
  coverage_id: "security-coverage--1",
  at: "2026-11-21T10:00:00Z",
};

function ev(over: Partial<CtemCaseEvent>): CtemCaseEvent {
  return { at: "2026-11-21T10:01:00Z", actor: "system", kind: "gate", issue: null, from: "ctem.analyst_review", to: null, reason: null, evidence: null, ...over };
}

beforeEach(() => {
  state.items = [];
  state.calls = [];
});

describe("case-review helpers", () => {
  it("keys the waiting review by case, the key both personas use", () => {
    expect(reviewDecisionKey("c1")).toBe("ctem:review:c1");
  });

  it("reads the stage from the package", () => {
    expect(reviewStage(null)).toBe("not_started");
    expect(reviewStage({ coverage_requested: false })).toBe("drafted");
    expect(reviewStage({ coverage_requested: true })).toBe("coverage_requested");
  });

  it("never reads coverage requested while the package sits in its draft (#1480)", () => {
    expect(reviewStage({ state: "drafted", coverage_requested: false, coverage_drafted: false })).toBe("drafted");
    expect(reviewStage({ state: "drafted", coverage_requested: false, coverage_drafted: true })).toBe("awaiting_validation");
    expect(reviewStage({ state: "validated", coverage_requested: false, coverage_drafted: false })).toBe("validated");
    expect(reviewStage({ state: "validated", coverage_requested: true, coverage_drafted: false })).toBe("coverage_requested");
  });

  it("reads a review answered without approval as re-plan or refused, whatever the page (#1573)", () => {
    expect(reviewStage({ state: "replan", coverage_requested: false, coverage_drafted: false })).toBe("replan");
    expect(reviewStage({ state: "rejected", coverage_requested: false, coverage_drafted: false })).toBe("rejected");
    expect(reviewStage({ state: "awaiting_review", coverage_requested: false, coverage_drafted: true })).toBe("awaiting_validation");
    expect(reviewStage({ state: "awaiting_review", coverage_requested: false, coverage_drafted: false })).toBe("drafted");
  });

  it("names a person by id, the OpenCTI mark as the mark, anything else as unknown", () => {
    expect(reviewAnswerer(USER)).toEqual({ kind: "person", id: USER });
    expect(reviewAnswerer("OpenCTI analyst mark")).toEqual({ kind: "mark" });
    expect(reviewAnswerer("Approved by the analyst's mark 'analyst-reviewed' in OpenCTI")).toEqual({ kind: "mark" });
    expect(reviewAnswerer("")).toEqual({ kind: "unknown" });
  });
});

describe("CaseAnalystReview", () => {
  it("shows the Grouping link, the coverage request and the review skipped by policy", () => {
    const history = [ev({ to: "passed_automatically", reason: "level low" })];
    renderWithProviders(<CaseAnalystReview caseId="c1" pkg={PKG} history={history} />);
    expect(screen.getByTestId("ctem-case-analyst-review-gate").textContent).toContain("Analyst review skipped by policy");
    expect(screen.getByText("Coverage requested in OpenCTI")).toBeTruthy();
    const link = screen.getByRole("link", { name: "Open threat package" }) as HTMLAnchorElement;
    expect(link.href).toBe(PKG.url);
    expect(link.target).toBe("_blank");
    expect(link.rel).toBe("noopener noreferrer");
    expect(screen.getByTestId("ctem-case-threat-package-name").textContent).toContain("CVE-2026-1234 on nginx");
    expect(screen.queryByText(/level low/)).toBeNull();
    expect(state.calls.every((c) => (c as { enabled: boolean }).enabled === false)).toBe(true);
  });

  it("renders a waiting review on the unified decision card", () => {
    // Even an item that names its case: inside the case, the card never offers "Open the case" (#2370).
    const ctem_case = { id: "c1", title: "Case", state: "validating", path: "/ctem/cases/c1" };
    state.items = [{ id: "w1", status: "blocked", needs_human: true, ask: { question: "Request it?", options: [] }, is_company_managed: false, ctem_case }];
    renderWithProviders(<CaseAnalystReview caseId="c1" pkg={{ ...PKG, coverage_requested: false, coverage_id: null }} history={[ev({ to: "waited" })]} />);
    expect(screen.getByText("Waiting for a person")).toBeTruthy();
    expect(screen.getByTestId("unified-decision-card").getAttribute("data-in-case")).toBe("true");
    expect(screen.getByText("Threat package drafted, coverage not requested yet")).toBeTruthy();
    expect(state.calls.some((c) => (c as { filters: { correlation_key: string } }).filters.correlation_key === "ctem:review:c1")).toBe(true);
  });

  it("points to the inbox or the OpenCTI label when the decision item is not visible", () => {
    renderWithProviders(<CaseAnalystReview caseId="c1" pkg={PKG} history={[ev({ to: "waited" })]} />);
    expect(screen.getByTestId("ctem-case-analyst-review-elsewhere")).toBeTruthy();
  });

  it("names the analyst's mark, never 'a person', when the mark answered", () => {
    const history = [ev({ to: "answered", reason: "OpenCTI analyst mark" })];
    renderWithProviders(<CaseAnalystReview caseId="c1" pkg={PKG} history={history} />);
    expect(screen.getByTestId("ctem-case-analyst-review-gate").textContent).toContain("by the analyst's mark in OpenCTI");
  });

  it("names the person who answered", () => {
    const history = [ev({ to: "answered", reason: USER })];
    renderWithProviders(<CaseAnalystReview caseId="c1" pkg={PKG} history={history} personName={(id) => (id === USER ? "Ada" : null)} />);
    expect(screen.getByTestId("ctem-case-analyst-review-gate").textContent).toContain("by Ada");
  });

  it("shows a drafted package with its coverage as not sent to OpenAEV, and the overdue signal past the SLA (#1480)", () => {
    const DRAFT = `https://cti.example.test/dashboard/data/import/draft/${GROUPING}`;
    const pkg = { ...PKG, url: DRAFT, state: "drafted" as const, coverage_requested: false, coverage_drafted: true, draft_overdue: true };
    renderWithProviders(<CaseAnalystReview caseId="c1" pkg={pkg} history={[]} />);
    expect(screen.getByTestId("ctem-case-threat-package-stage").textContent).toBe("Threat package drafted, not validated: coverage not sent to OpenAEV");
    expect(screen.queryByText("Coverage requested in OpenCTI")).toBeNull();
    expect(screen.getByTestId("ctem-case-threat-package-overdue").textContent).toContain("past the case SLA");
    expect((screen.getByTestId("ctem-case-threat-package-link") as HTMLAnchorElement).href).toBe(DRAFT);
  });

  it("shows no overdue signal on a validated package", () => {
    renderWithProviders(<CaseAnalystReview caseId="c1" pkg={{ ...PKG, state: "validated", draft_overdue: false }} history={[]} />);
    expect(screen.getByTestId("ctem-case-threat-package-stage").textContent).toBe("Coverage requested in OpenCTI");
    expect(screen.queryByTestId("ctem-case-threat-package-overdue")).toBeNull();
  });

  it("keeps the drafted state for a viewer who does not read OpenCTI", () => {
    renderWithProviders(
      <CaseAnalystReview caseId="c1" pkg={{ restricted: true, state: "drafted", coverage_requested: false, coverage_drafted: true, draft_overdue: false, at: PKG.at }} history={[]} />,
    );
    expect(screen.getByTestId("ctem-case-threat-package-stage").textContent).toBe("Threat package drafted, not validated: coverage not sent to OpenAEV");
  });

  it("keeps the Grouping restricted for a viewer who does not read OpenCTI", () => {
    renderWithProviders(<CaseAnalystReview caseId="c1" pkg={{ restricted: true, coverage_requested: true, at: PKG.at }} history={[]} />);
    expect(screen.getByText("A Grouping exists in OpenCTI: restricted to OpenCTI readers")).toBeTruthy();
    expect(screen.queryByTestId("ctem-case-threat-package-link")).toBeNull();
    const button = screen.getByRole("button", { name: "Open threat package" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.getAttribute("aria-describedby")).toBe("ctem-case-threat-package-restricted");
  });

  it("draws no button when the package has no URL", () => {
    renderWithProviders(<CaseAnalystReview caseId="c1" pkg={{ ...PKG, url: null }} history={[]} />);
    expect(screen.queryByRole("link", { name: "Open threat package" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Open threat package" })).toBeNull();
    expect(screen.getByTestId("ctem-case-threat-package-name").textContent).toContain("CVE-2026-1234 on nginx");
  });

  // The package keeps one slot, first in the card, whatever the gate state or the history order (#2239).
  function packageComesFirst() {
    const card = screen.getByTestId("ctem-case-analyst-review");
    const content = screen.getByTestId("ctem-case-threat-package").parentElement as HTMLElement;
    expect(card.contains(content)).toBe(true);
    expect(content.firstElementChild).toBe(screen.getByTestId("ctem-case-threat-package"));
  }

  it("keeps the package above the waiting decision card", () => {
    state.items = [{ id: "w1", status: "blocked", needs_human: true, ask: { question: "Request it?", options: [] }, is_company_managed: false }];
    renderWithProviders(<CaseAnalystReview caseId="c1" pkg={PKG} history={[ev({ to: "waited" })]} />);
    packageComesFirst();
    const pkgBlock = screen.getByTestId("ctem-case-threat-package");
    const decision = screen.getByTestId("unified-decision-card");
    expect(pkgBlock.compareDocumentPosition(decision) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("keeps the package above the gate line for an answered or skipped review, in any history order", () => {
    const answered = ev({ at: "2026-11-21T10:05:00Z", to: "answered", reason: USER });
    const waited = ev({ at: "2026-11-21T10:02:00Z", to: "waited" });
    for (const history of [[waited, answered], [answered, waited], [ev({ to: "passed_automatically" })]]) {
      const { unmount } = renderWithProviders(<CaseAnalystReview caseId="c1" pkg={PKG} history={history} />);
      packageComesFirst();
      const gateLine = screen.queryByTestId("ctem-case-analyst-review-gate");
      if (gateLine) expect(screen.getByTestId("ctem-case-threat-package").compareDocumentPosition(gateLine) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      unmount();
    }
  });

  it("keeps the package first with no gate at all", () => {
    renderWithProviders(<CaseAnalystReview caseId="c1" pkg={PKG} history={[]} />);
    packageComesFirst();
  });

  it("is left out of the page while there is no package and no review gate (#2786)", () => {
    renderWithProviders(<CaseAnalystReview caseId="c1" pkg={null} history={[]} />);
    expect(screen.queryByTestId("ctem-case-analyst-review")).toBeNull();
  });

  it("shows a waiting review with no package yet (#2786)", () => {
    renderWithProviders(<CaseAnalystReview caseId="c1" pkg={null} history={[ev({ to: "waited" })]} />);
    expect(screen.getByTestId("ctem-case-analyst-review")).toBeTruthy();
  });
});
