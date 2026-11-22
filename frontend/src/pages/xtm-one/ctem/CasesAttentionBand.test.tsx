// SPDX-License-Identifier: Apache-2.0

import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { renderWithProviders } from "@/testing/providers";
import { CasesAttentionBand } from "./CasesAttentionBand";

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

const band = (cases: CtemCaseSummary[]) =>
  renderWithProviders(
    <MemoryRouter>
      <CasesAttentionBand cases={cases} now={NOW} />
    </MemoryRouter>,
  );

function kase(over: Partial<CtemCaseSummary>): CtemCaseSummary {
  return {
    id: "c-1",
    title: "Upgrade nginx",
    priority: "critical",
    state: "needs_attention",
    open_issue_count: 3,
    restricted_count: 0,
    crown_jewel_count: 0,
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
