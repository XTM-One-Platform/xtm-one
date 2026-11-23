// The agentic checks line of an OpenCTI, OpenAEV or OpenCRQ card (#101 S15e, section 13.6): the
// counts of the latest round on every registration, plural-safe at 0 and 1, and every check with its
// verdict in the shared popover, failed first.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { HealthModuleCheckReport } from "@/types";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
const registrations = [
  { id: "r-1", platform_title: "OpenCTI Paris", platform_url: "https://cti-paris.example" },
  { id: "r-2", platform_title: "OpenCTI Lyon", platform_url: "https://cti-lyon.example" },
];
const answer = (checks: unknown) => (path: string) => Promise.resolve(path === "/platform/registrations" ? registrations : checks);
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

  it("sums every registration's latest round, keeps the newest end, and groups the checks by registration, failed first", () => {
    const healthy = report({
      registration_id: "r-2",
      finished_at: "2026-11-22T11:00:00Z",
      passed: 1,
      warned: 0,
      failed: 0,
      not_run: 0,
      checks: [{ id: "feed_dead_url", verdict: "pass", category: "configuration", recommendation: null, reason: null, evidence: [], finding_id: null }],
    });
    const totals = checkRoundTotals([healthy, report()]);
    expect(totals).toMatchObject({ passed: 2, warned: 1, failed: 1, notRun: 0, finishedAt: "2026-11-22T11:00:00Z" });
    expect(totals?.groups.map((group) => group.registrationId)).toEqual(["r-1", "r-2"]);
    expect(totals?.groups[0].checks[0].verdict).toBe("fail");
  });
});

describe("ModuleCheckReportLine", () => {
  beforeEach(() => vi.clearAllMocks());

  it("says when no check round ran yet", async () => {
    api.get.mockImplementation(answer({ module: "opencti", items: [] }));
    renderWithProviders(<ModuleCheckReportLine module="opencti" />);
    expect(await screen.findByText("No check round yet")).toBeTruthy();
    expect(api.get).toHaveBeenCalledWith("/health/modules/opencti/checks");
  });

  it.each(["not_set", "no_access"])("shows one run-as state and its setting link when run_as is %s (dev-docs 12.3)", async (state) => {
    api.get.mockImplementation(answer({ module: "openaev", items: [report()], run_as: state }));
    renderWithProviders(
      <MemoryRouter>
        <ModuleCheckReportLine module="openaev" hideEmpty />
      </MemoryRouter>,
    );
    const line = await screen.findByTestId("health-checks-run-as");
    expect(line).toHaveTextContent("Checks need a run-as user with OpenAEV access.");
    expect(screen.getByRole("link", { name: "Set the run-as user" }).getAttribute("href")).toBe("/settings/automations/health#health-settings-run-as");
    expect(screen.queryByTestId("health-checks-counts")).toBeNull();
    expect(screen.queryByText(/not run/)).toBeNull();
  });

  it("renders nothing with no round under the Last check round block (UX round 1 B1 on #1269)", async () => {
    api.get.mockImplementation(answer({ module: "opencti", items: [] }));
    renderWithProviders(<ModuleCheckReportLine module="opencti" hideEmpty />);
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledWith("/health/modules/opencti/checks"));
    expect(screen.queryByText("No check round yet")).toBeNull();
    expect(screen.queryByTestId("health-checks-opencti")).toBeNull();
  });

  it("keeps the counts under the Last check round block once a round reported", async () => {
    api.get.mockImplementation(answer({ module: "openaev", items: [report({ passed: 1, warned: 0, failed: 1, not_run: 1 })] }));
    renderWithProviders(<ModuleCheckReportLine module="openaev" hideEmpty />);
    expect((await screen.findByTestId("health-checks-counts")).textContent).toBe("1 passed, 0 warnings, 1 failed, 1 not run");
  });

  it("counts in the singular at 1 and with the count at 0", async () => {
    api.get.mockImplementation(answer({ module: "openaev", items: [report({ passed: 1, warned: 0, failed: 1, not_run: 1 })] }));
    renderWithProviders(<ModuleCheckReportLine module="openaev" />);
    expect((await screen.findByTestId("health-checks-counts")).textContent).toBe("1 passed, 0 warnings, 1 failed, 1 not run");
  });

  it("lists every check with its verdict in plain words and the recommendation of a failed one", async () => {
    api.get.mockImplementation(answer({ module: "opencti", items: [report()] }));
    renderWithProviders(<ModuleCheckReportLine module="opencti" />);
    expect(await screen.findByRole("heading", { name: "Agentic checks" })).toBeTruthy();
    expect((await screen.findByTestId("health-checks-meta")).textContent).toContain("As of");
    await userEvent.setup().click(await screen.findByTestId("health-checks-expand-opencti"));
    const rows = await screen.findAllByTestId("health-check-row");
    expect(rows).toHaveLength(3);
    expect(screen.queryByTestId("health-check-group")).toBeNull();
    expect(rows[0].textContent).toContain("Failed");
    expect(rows[0].textContent).toContain("Feed dead url");
    expect(rows[0].textContent).toContain("Point the feed at a live URL.");
    expect(rows[2].textContent).toContain("Passed");
    expect(rows[2].textContent).not.toContain("pass ");
  });

  it("names each registration over its own checks when the module has several", async () => {
    const healthy = report({
      registration_id: "r-2",
      passed: 1,
      warned: 0,
      failed: 0,
      checks: [{ id: "feed_dead_url", verdict: "pass", category: "configuration", recommendation: null, reason: null, evidence: [], finding_id: null }],
    });
    api.get.mockImplementation(answer({ module: "opencti", items: [healthy, report()] }));
    renderWithProviders(<ModuleCheckReportLine module="opencti" />);
    await userEvent.setup().click(await screen.findByTestId("health-checks-expand-opencti"));
    const groups = await screen.findAllByTestId("health-check-group");
    expect(groups).toHaveLength(2);
    expect(groups[0].textContent).toContain("OpenCTI Paris");
    expect(groups[0].textContent).toContain("Failed");
    expect(groups[1].textContent).toContain("OpenCTI Lyon");
    expect(groups[1].textContent).not.toContain("Failed");
  });
});
