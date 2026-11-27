// SPDX-License-Identifier: Apache-2.0

import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { renderWithProviders } from "@/testing/providers";
import { CaseAttentionNow } from "./CaseAttentionNow";
import { CaseLoopLine } from "./case-loop";

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

const render = (c: CtemCaseSummary) =>
  renderWithProviders(
    <MemoryRouter>
      <CaseAttentionNow c={c} now={NOW} onChanged={() => undefined} />
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
    primary_asset_count: 2,
    kev_count: 1,
    asset_count: 4,
    owner_user_id: null,
    due_at: null,
    created_at: "2026-11-19T12:00:00Z",
    updated_at: "2026-11-21T12:00:00Z",
    state_since: "2026-11-21T12:00:00Z",
    ...over,
  } as CtemCaseSummary;
}

describe("CaseAttentionNow (#1176)", () => {
  it("says why an open case needs attention, with the list's words, and offers its next action", () => {
    render(kase({}));
    const why = screen.getByTestId("ctem-case-attention-why").textContent ?? "";
    expect(why).toContain("1 KEV on 4 assets");
    expect(why).toContain("2 primary assets touched");
    expect(why).toContain("No owner");
    expect(why).toContain("Opened 3 days ago");
    expect(screen.getByTestId("ctem-cases-single-assign").textContent).toBe("Assign owner");
  });

  it("shows created, age and last change", () => {
    render(kase({}));
    expect(screen.getByTestId("ctem-case-age").textContent).toContain("3 days");
    expect(screen.getByTestId("ctem-case-created").textContent).not.toMatch(/-$/);
    expect(screen.getByTestId("ctem-case-last-change").textContent).not.toContain("-");
  });

  it("says 1 day in the singular", () => {
    render(kase({ created_at: "2026-11-21T11:00:00Z" }));
    expect(screen.getByTestId("ctem-case-age").textContent).toContain("1 day");
    expect(screen.getByTestId("ctem-case-age").textContent).not.toContain("1 days");
  });

  it("titles the band with the shared h3 section header (UX r1 B1)", () => {
    render(kase({}));
    expect(screen.getByRole("heading", { level: 3, name: "Why this case needs attention now" })).toBeTruthy();
    expect(screen.getByTestId("ctem-case-attention-title")).toBeTruthy();
  });

  it("labels the date Opened, like the why line, and says Today on the day it opened (UX r1 B2)", () => {
    render(kase({ created_at: "2026-11-22T08:00:00Z" }));
    expect(screen.getByTestId("ctem-case-created").textContent).toContain("Opened");
    expect(screen.getByTestId("ctem-case-created").textContent).not.toContain("Created");
    expect(screen.getByTestId("ctem-case-age").textContent).toBe("AgeToday");
    expect(screen.getByTestId("ctem-case-attention-why").textContent).toContain("Opened today");
  });

  // #2633: the header chip and the why line read the same countdown in the same words.
  it.each([
    ["2026-11-25T11:00:00Z", "2 d 23 h left"],
    ["2026-11-23T11:00:00Z", "23 h left"],
  ])("says the SLA due %s as %s in the chip and in the why line", (due, text) => {
    const c = kase({ sla_due_at: due, severity: "P2" });
    renderWithProviders(
      <MemoryRouter>
        <CaseLoopLine c={c} now={NOW} />
        <CaseAttentionNow c={c} now={NOW} onChanged={() => undefined} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("ctem-case-sla").textContent).toBe(text);
    expect(screen.getByTestId("ctem-case-attention-why").textContent).toContain(text);
  });

  it("shows the dates alone on a resolved case: nothing to act on", () => {
    render(kase({ state: "resolved" }));
    expect(screen.queryByTestId("ctem-case-attention-why")).toBeNull();
    expect(screen.queryByTestId("ctem-case-attention-next")).toBeNull();
    expect(screen.getByTestId("ctem-case-created")).toBeTruthy();
  });
});
