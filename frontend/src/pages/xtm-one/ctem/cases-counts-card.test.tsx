// The CTEM Cases row of the command center (#11545 S5a, dev-docs/ctem-cases.md
// 7.4): the four state counts from the list's own counts, each opening the
// Cases list on that state, overdue and expiring links on the same definition
// as the list's Due filter, a sentence when there is no case, the setup row
// when nothing can build cases, a retry when the read failed.

import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient } from "@tanstack/react-query";

import { ErrorBoundary } from "@/components/ui/error-boundary";
import { api, BackendUnavailableError } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { ctemCasesPollInterval, ctemCasesRetry } from "@/hooks/api/useCtemCasesApi";
import { OUTAGE_QUERY_RETRIES } from "@/lib/query-outage-policy";
import { CasesCountsCard } from "./CasesCountsCard";
import { caseDueFlags, casesListHref } from "./cases-due";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn() },
}));

const get = vi.mocked(api.get);
const DAY = 24 * 3600 * 1000;

function draw(configured = true, onConnectOpenAev = () => {}) {
  return renderWithProviders(
    <MemoryRouter>
      <CasesCountsCard configured={configured} onConnectOpenAev={onConnectOpenAev} />
    </MemoryRouter>,
  );
}

function summary(over: Record<string, unknown>) {
  return { id: "c", state: "new", due_at: null, accepted_until: null, ...over };
}

afterEach(() => get.mockReset());

describe("CasesCountsCard", () => {
  it("shows one count per state, each linking to the list on that state", async () => {
    get.mockResolvedValue({
      vertical: "cyber",
      counts: { new: 18, remediating: 4, resolved: 22, accepted_risk: 3 },
      cases: [],
      details_unavailable: [],
    });
    draw();
    const fresh = await screen.findByRole("link", { name: "New: 18 cases" });
    expect(fresh.getAttribute("href")).toBe("/ctem/cases?state=new");
    expect(screen.getByRole("link", { name: "Remediating: 4 cases" }).getAttribute("href")).toBe("/ctem/cases?state=remediating");
    expect(screen.getByRole("link", { name: "Resolved: 22 cases" }).getAttribute("href")).toBe("/ctem/cases?state=resolved");
    expect(screen.getByRole("link", { name: "Accepted risk: 3 cases" }).getAttribute("href")).toBe("/ctem/cases?state=accepted_risk");
    expect(screen.getByRole("link", { name: "Open Cases" }).getAttribute("href")).toBe("/ctem/cases");
    expect(screen.queryByTestId("ctem-cases-overdue")).toBeNull();
    expect(screen.queryByTestId("ctem-cases-expiring")).toBeNull();
  });

  it("links the decisions waiting to the case that waits longest, absent at 0 (#1621)", async () => {
    get.mockImplementation(async (path: string) =>
      path === "/ctem-cases/overview"
        ? { waiting: { count: 2, oldest: { case_id: "c-old", title: "Old", phase: "remediation", gate: "approve_fix", since: null }, oldest_hours: 5.2, cases: [] } }
        : { vertical: "cyber", counts: { waiting_decision: 2 }, cases: [], details_unavailable: [] },
    );
    draw();
    const waiting = await screen.findByTestId("ctem-cases-waiting");
    expect(waiting.textContent).toBe("Waiting for a decision: 2, oldest 5 h");
    expect(waiting.getAttribute("href")).toBe("/ctem/cases/c-old");
  });

  it("draws no waiting line when no case waits for a decision (#1621)", async () => {
    get.mockImplementation(async (path: string) =>
      path === "/ctem-cases/overview"
        ? { waiting: { count: 0, oldest: null, oldest_hours: null, cases: [] } }
        : { vertical: "cyber", counts: { new: 1 }, cases: [], details_unavailable: [] },
    );
    draw();
    await screen.findByRole("link", { name: "New: 1 case" });
    expect(screen.queryByTestId("ctem-cases-waiting")).toBeNull();
  });

  it("draws one count link per state, mitigated, watching included", async () => {
    get.mockResolvedValue({ vertical: "cyber", counts: { new: 1, mitigated_watching: 1 }, cases: [], details_unavailable: [] });
    draw();
    await screen.findByRole("link", { name: "New: 1 case" });
    expect(screen.getAllByTestId(/^ctem-cases-count-/)).toHaveLength(8);
  });

  it("a state the API leaves out counts zero, never a dash", async () => {
    get.mockResolvedValue({ vertical: "cyber", counts: { new: 1 }, cases: [], details_unavailable: [] });
    draw();
    expect(await screen.findByRole("link", { name: "Accepted risk: 0 cases" })).toBeTruthy();
  });

  it("links the overdue and expiring cases to the list's Due filter", async () => {
    const now = Date.now();
    get.mockResolvedValue({
      vertical: "cyber",
      counts: { new: 2, accepted_risk: 1 },
      cases: [
        summary({ id: "a", due_at: new Date(now - DAY).toISOString() }),
        summary({ id: "b", due_at: new Date(now + DAY).toISOString() }),
        summary({ id: "c", state: "accepted_risk", accepted_until: new Date(now + 3 * DAY).toISOString() }),
      ],
      details_unavailable: [],
    });
    draw();
    const overdue = await screen.findByTestId("ctem-cases-overdue");
    expect(overdue.textContent).toBe("1 overdue");
    expect(overdue.getAttribute("href")).toBe("/ctem/cases?state=all&c_f_due=overdue");
    const expiring = screen.getByTestId("ctem-cases-expiring");
    expect(expiring.textContent).toBe("1 accepted risk expires in 14 days");
    expect(expiring.getAttribute("href")).toBe("/ctem/cases?state=all&c_f_due=expiring");
  });

  it("says so when there is no case", async () => {
    get.mockResolvedValue({ vertical: "cyber", counts: {}, cases: [], details_unavailable: [] });
    draw();
    expect(await screen.findByText("No case yet: cases appear when open issues on your software share a fix.")).toBeTruthy();
  });

  it("offers the setup row when nothing can build cases, and closes it", async () => {
    get.mockResolvedValue({ vertical: "cyber", counts: {}, cases: [], details_unavailable: [] });
    const connect = vi.fn();
    draw(false, connect);
    expect(await screen.findByText("Build cases from your software")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Add software" }).getAttribute("href")).toBe("/ctem/inventory/software?card=tech_stack");
    fireEvent.click(screen.getByRole("button", { name: "Connect OpenAEV" }));
    expect(connect).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByText("Build cases from your software")).toBeNull();
  });

  it("offers a retry when the read failed", async () => {
    get.mockRejectedValue(new Error("boom"));
    draw();
    const retry = await screen.findByRole("button", { name: "Retry" });
    get.mockResolvedValue({ vertical: "cyber", counts: { new: 2 }, cases: [], details_unavailable: [] });
    fireEvent.click(retry);
    expect(await screen.findByRole("link", { name: "New: 2 cases" })).toBeTruthy();
  });

  it("keeps a failed read in its row on the app's client: the page and its other rows stay mounted", async () => {
    get.mockRejectedValue(new Error("500"));
    // The app's own client: any failed query throws to the nearest boundary.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, throwOnError: true } } });
    renderWithProviders(
      <MemoryRouter>
        <ErrorBoundary fallback={<p>Failed to load this section</p>}>
          <p>Act now row</p>
          <CasesCountsCard configured onConnectOpenAev={() => {}} />
        </ErrorBoundary>
      </MemoryRouter>,
      client,
    );
    expect(await screen.findByRole("button", { name: "Retry" })).toBeTruthy();
    expect(screen.getByText("Cases could not be read.", { exact: false })).toBeTruthy();
    expect(screen.getByText("Act now row")).toBeTruthy();
    expect(screen.queryByText("Failed to load this section")).toBeNull();
  });
});

describe("the case list read (#11999)", () => {
  it("a failed read is one request: no automatic retry", async () => {
    get.mockRejectedValue(Object.assign(new Error("Not Found"), { status: 404 }));
    draw();
    expect(await screen.findByRole("button", { name: "Retry" })).toBeTruthy();
    // One request to the list; the overview (#1621) is its own read.
    expect(get.mock.calls.filter(([path]) => path !== "/ctem-cases/overview")).toHaveLength(1);
  });

  it("stops polling after a read the server refused", () => {
    const notFound = Object.assign(new Error("Not Found"), { status: 404 });
    expect(ctemCasesPollInterval("error", notFound)).toBe(false);
    expect(ctemCasesPollInterval("success")).toBe(30_000);
  });

  it("never retries a read the server refused", () => {
    expect(ctemCasesRetry(0, Object.assign(new Error("Not Found"), { status: 404 }))).toBe(false);
    expect(ctemCasesRetry(0, Object.assign(new Error("Server error"), { status: 500 }))).toBe(false);
  });

  it("keeps the app's outage policy and the poll on a network error", () => {
    const outage = new TypeError("Failed to fetch");
    expect(ctemCasesRetry(0, outage)).toBe(true);
    expect(ctemCasesRetry(OUTAGE_QUERY_RETRIES, outage)).toBe(false);
    expect(ctemCasesRetry(0, new BackendUnavailableError("Server unavailable (HTTP 502)"))).toBe(true);
    expect(ctemCasesPollInterval("error", outage)).toBe(30_000);
  });

  it("an empty list shows the empty state, never the failed read", async () => {
    get.mockResolvedValue({ vertical: "cyber", counts: { needs_attention: 0, in_progress: 0, resolved: 0, accepted_risk: 0 }, cases: [], details_unavailable: [] });
    draw();
    expect(await screen.findByText("No case yet: cases appear when open issues on your software share a fix.")).toBeTruthy();
    expect(screen.queryByText("Cases could not be read.", { exact: false })).toBeNull();
  });
});

describe("caseDueFlags", () => {
  const now = Date.parse("2026-11-13T12:00:00Z");
  it("an open case past its due date is overdue; a resolved one never", () => {
    expect(caseDueFlags({ state: "remediating", due_at: "2026-11-12T00:00:00Z", accepted_until: null }, now)).toEqual(["overdue"]);
    expect(caseDueFlags({ state: "resolved", due_at: "2026-11-12T00:00:00Z", accepted_until: null }, now)).toEqual([]);
  });
  it("an accepted risk ending within 14 days is expiring; past or later is not", () => {
    expect(caseDueFlags({ state: "accepted_risk", due_at: null, accepted_until: "2026-11-27T11:00:00Z" }, now)).toEqual(["expiring"]);
    expect(caseDueFlags({ state: "accepted_risk", due_at: null, accepted_until: "2026-11-28T13:00:00Z" }, now)).toEqual([]);
    expect(caseDueFlags({ state: "accepted_risk", due_at: null, accepted_until: "2026-11-12T00:00:00Z" }, now)).toEqual([]);
  });
  it("builds the list links", () => {
    expect(casesListHref({ state: "resolved" })).toBe("/ctem/cases?state=resolved");
    expect(casesListHref({ due: "overdue" })).toBe("/ctem/cases?state=all&c_f_due=overdue");
  });
});
