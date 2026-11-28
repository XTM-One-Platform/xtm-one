// SPDX-License-Identifier: Apache-2.0

// The case page carries an icon on every state, result, autonomy chip and empty
// state (#1679, point 5), and its fold buttons say one wording (advisory A1 of #1918).
import { describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { renderWithProviders } from "@/testing/providers";
import type { CtemCaseState } from "@/hooks/api/useCtemCasesApi";
import { AUTONOMY_CLASSES } from "@/lib/ctem-case-audit";
import { CaseStateChip } from "./cases-shared";
import { CaseAutonomyChip, CASE_SECTION_ICON } from "./case-icons";
import { CaseSectionReason } from "./CaseSectionReason";

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: false }) }));
vi.mock("@/hooks/api/useWorkItemApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useWorkItemApi")>()),
  useWorkItems: () => ({ data: undefined, isLoading: false, refetch: vi.fn() }),
}));

const { CaseRemediationDecision } = await import("./CaseRemediationDecision");

const STATES: CtemCaseState[] = [
  "new",
  "analyzing",
  "waiting_decision",
  "remediating",
  "validating",
  "mitigated_watching",
  "resolved",
  "accepted_risk",
];

describe("case page icons (#1679, point 5)", () => {
  it.each(STATES)("draws the %s state chip with its icon", (state) => {
    const { container } = renderWithProviders(<CaseStateChip state={state} />);
    expect(container.querySelector("svg")).not.toBeNull();
  });

  it.each([...AUTONOMY_CLASSES])("draws the %s autonomy chip with its icon and label", (autonomy) => {
    renderWithProviders(<CaseAutonomyChip autonomy={autonomy} testId="chip" />);
    const chip = screen.getByTestId("chip");
    expect(chip.querySelector("svg")).not.toBeNull();
    expect(chip.textContent?.trim().length).toBeGreaterThan(0);
  });

  it("puts the section icon before the sentence of an empty section", () => {
    renderWithProviders(
      <MemoryRouter>
        <CaseSectionReason own="What it holds" icon={CASE_SECTION_ICON.validation} testId="reason" />
      </MemoryRouter>,
    );
    const reason = screen.getByTestId("reason");
    expect(reason.querySelector("svg")).not.toBeNull();
    expect(within(reason).getByText("What it holds")).toBeInTheDocument();
  });

  it("draws no empty remediation decision card: a case not decided yet leaves it out (#2786)", () => {
    renderWithProviders(
      <MemoryRouter>
        <CaseRemediationDecision caseId="c1" history={[]} />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("ctem-case-remediation")).toBeNull();
  });
});
