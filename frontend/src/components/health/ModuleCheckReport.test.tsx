// The agentic checks line of an OpenCTI, OpenAEV or OpenCRQ card (#101 S15e, section 13.6): the
// counts of the latest round on every registration, plural-safe at 0 and 1, and every check with its
// verdict in the shared popover, failed first.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";
import type { HealthModuleCheckReport } from "@/types";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("@/lib/api", () => ({ api }));

import { ModuleCheckReportLine, checkRoundTotals } from "./ModuleCheckReport";

const report = (overrides: Partial<HealthModuleCheckReport> = {}): HealthModuleCheckReport => ({
  report_id: "rep-1",
  registration_id: "r-1",
  cycle_id: "c-1",
  started_at: "2026-11-22T10:00:00Z",
  finished_at: "2026-11-22T10:05:00Z",
  passed: 1,
  warned: 1,
  failed: 1,
  not_run: 0,
  checks: [
    { id: "connector_freshness", verdict: "pass", category: "configuration", recommendation: null, reason: null, evidence: [], finding_id: null },
    { id: "feed_dead_url", verdict: "fail", category: "configuration", recommendation: "Point the feed at a live URL.", reason: null, evidence: [], finding_id: "f-1" },
    { id: "queue_backlog", verdict: "warn", category: "performance", recommendation: "Add a worker.", reason: null, evidence: [], finding_id: "f-2" },
  ],
  ...overrides,
});

describe("checkRoundTotals", () => {
  it("is null when no round ran", () => {
    expect(checkRoundTotals([])).toBeNull();
  });

  it("sums every registration's latest round and keeps the newest end, failed checks first", () => {
    const totals = checkRoundTotals([report(), report({ registration_id: "r-2", finished_at: "2026-11-22T11:00:00Z", passed: 4, warned: 0, failed: 0, not_run: 2 })]);
    expect(totals).toMatchObject({ passed: 5, warned: 1, failed: 1, notRun: 2, finishedAt: "2026-11-22T11:00:00Z" });
    expect(totals?.checks[0].verdict).toBe("fail");
  });
});

describe("ModuleCheckReportLine", () => {
  beforeEach(() => vi.clearAllMocks());

  it("says when no check round ran yet", async () => {
    api.get.mockResolvedValue({ module: "opencti", items: [] });
    renderWithProviders(<ModuleCheckReportLine module="opencti" />);
    expect(await screen.findByText("No check round yet")).toBeTruthy();
    expect(api.get).toHaveBeenCalledWith("/health/modules/opencti/checks");
  });

  it("counts in the singular at 1 and with the count at 0", async () => {
    api.get.mockResolvedValue({ module: "openaev", items: [report({ passed: 1, warned: 0, failed: 1, not_run: 1 })] });
    renderWithProviders(<ModuleCheckReportLine module="openaev" />);
    expect((await screen.findByTestId("health-checks-counts")).textContent).toBe("1 passed, 0 warnings, 1 failed, 1 not run");
  });

  it("lists every check with its verdict in plain words and the recommendation of a failed one", async () => {
    api.get.mockResolvedValue({ module: "opencti", items: [report()] });
    renderWithProviders(<ModuleCheckReportLine module="opencti" />);
    await userEvent.setup().click(await screen.findByTestId("health-checks-expand-opencti"));
    const rows = await screen.findAllByTestId("health-check-row");
    expect(rows).toHaveLength(3);
    expect(rows[0].textContent).toContain("Failed");
    expect(rows[0].textContent).toContain("Feed dead url");
    expect(rows[0].textContent).toContain("Point the feed at a live URL.");
    expect(rows[2].textContent).toContain("Passed");
    expect(rows[2].textContent).not.toContain("pass ");
  });
});
