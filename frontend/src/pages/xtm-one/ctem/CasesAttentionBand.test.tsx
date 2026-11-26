// SPDX-License-Identifier: Apache-2.0

import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { renderWithProviders } from "@/testing/providers";
import { CasesAttentionBand } from "./CasesAttentionBand";
import { DEFAULT_CASE_ATTENTION_WEIGHTS, type CaseAttentionWeights } from "./case-attention";

const auth = vi.hoisted(() => ({ isAdmin: false }));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => auth }));
vi.mock("@/hooks/api/useOrganizationContextApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useOrganizationContextApi")>()),
  useOrganizationContext: () => ({ data: undefined }),
  usePatchOrganizationContext: () => ({ isPending: false, mutate: () => undefined }),
}));

vi.mock("@/hooks/api/useAuthApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useAuthApi")>()),
  useMe: () => ({ data: { id: "u1" } }),
  useUserDirectory: () => ({ data: [] }),
}));

vi.mock("@/hooks/api/useCtemCasesApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemCasesApi")>()),
  useBulkUpdateCtemCases: () => ({ isPending: false, mutate: () => undefined, mutateAsync: () => Promise.resolve({ updated: [], failed: [] }) }),
  useMergeCtemCases: () => ({ isPending: false, mutate: () => undefined }),
}));

const NOW = Date.parse("2026-11-22T12:00:00Z");

const band = (cases: CtemCaseSummary[], weights: CaseAttentionWeights = DEFAULT_CASE_ATTENTION_WEIGHTS) =>
  renderWithProviders(
    <MemoryRouter>
      <CasesAttentionBand cases={cases} now={NOW} weights={weights} />
    </MemoryRouter>,
  );

function kase(over: Partial<CtemCaseSummary>): CtemCaseSummary {
  return {
    id: "c-1",
    title: "Upgrade nginx",
    priority: "critical",
    state: "new",
    open_issue_count: 3,
    restricted_count: 0,
    primary_asset_count: 0,
    kev_count: 1,
    asset_count: 0,
    owner_user_id: null,
    due_at: null,
    state_since: "2026-11-22T10:00:00Z",
    ...over,
  } as CtemCaseSummary;
}

describe("CasesAttentionBand (UX round 1 on #1182)", () => {
  it("says the KEV count alone when no asset is known, never 'on 0 assets' (B2)", () => {
    band([kase({})]);
    const why = screen.getByTestId("ctem-cases-attention-why");
    expect(why.textContent).toContain("1 KEV");
    expect(why.textContent).not.toContain("0 assets");
  });

  it("names the assets when they are known", () => {
    band([kase({ asset_count: 4 })]);
    expect(screen.getByTestId("ctem-cases-attention-why").textContent).toContain("1 KEV on 4 assets");
  });

  it("opens the Assign owner control of the bulk actions for an unowned case (B3)", () => {
    band([kase({})]);
    expect(screen.getByTestId("ctem-cases-single-assign").textContent).toBe("Assign owner");
  });

  it("opens Set due date for an owned case with no due date (B3)", () => {
    band([kase({ owner_user_id: "u-1" })]);
    expect(screen.getByTestId("ctem-cases-single-due").textContent).toBe("Set due date");
  });
});

describe("CasesAttentionBand ranking points (#1176 slice 2b)", () => {
  it("ranks with the organization's points", () => {
    const cases = [kase({ id: "crit", title: "Critical one", kev_count: 0 }), kase({ id: "kev", title: "KEV one", priority: "low", kev_count: 5 })];
    band(cases, { ...DEFAULT_CASE_ATTENTION_WEIGHTS, priority: { critical: 0, high: 0, medium: 0, low: 0 }, kev: 50 });
    expect(screen.getAllByTestId("ctem-cases-attention-row")[0].textContent).toContain("KEV one");
  });

  it("offers Ranking points to an administrator only", () => {
    auth.isAdmin = false;
    const { unmount } = band([kase({})]);
    expect(screen.queryByTestId("ctem-cases-attention-weights-open")).toBeNull();
    unmount();
    auth.isAdmin = true;
    band([kase({})]);
    expect(screen.getByTestId("ctem-cases-attention-weights-open").textContent).toContain("Ranking points");
    auth.isAdmin = false;
  });
});
