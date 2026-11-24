// The Activity tab of a relay group is drawn by the shared DataTable
// (dev-docs/reference/data-tables.md, slice 7 of #9378): the server searches by
// host, filters by outcome and relay, orders by date and pages; the state lives
// in the URL (act_), the CSV export keeps the raw values.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { TABLE_STORAGE_PREFIX } from "@/lib/data-table-state";
import { renderWithProviders } from "@/testing/providers";
import type { RelayGroup, RelayRequestLog } from "@/types";
import { ActivityTab } from "./ActivityTab";
import { relayOutcomeParam as outcomeParam, relayRequestFailed as failed } from "./relay-labels";

const entry = (id: string, overrides: Partial<RelayRequestLog> = {}): RelayRequestLog =>
  ({
    id,
    group_id: "g-1",
    relay_id: "r-1",
    method: "GET",
    scheme: "https",
    host: `${id}.internal.example`,
    port: null,
    path: "/api/v1/health",
    status_code: 200,
    error_code: null,
    duration_ms: 42,
    bytes_sent: 512,
    bytes_received: 2048,
    source_type: "integration",
    created_at: "2026-11-10T08:00:00+00:00",
    ...overrides,
  }) as RelayRequestLog;

const calls: Record<string, unknown>[] = [];
const options: Record<string, unknown>[] = [];
let PAGE_STATE: { isError: boolean } = { isError: false };
const PAGE = {
  data: { items: [entry("ok-1"), entry("err-1", { status_code: null, error_code: "dns_failed" })], total: 2 },
  isLoading: false,
  isError: false,
  refetch: vi.fn(),
};
const RELAYS = { data: [{ id: "r-1", name: "Paris DC" }] };

vi.mock("@/hooks/api/useRelayApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useRelayApi")>()),
  useGroupRelays: () => RELAYS,
  useRelayGroupRequests: (_id: string, params: Record<string, unknown>, opts: Record<string, unknown> = {}) => {
    calls.push(params);
    options.push(opts);
    return PAGE_STATE.isError ? { ...PAGE, data: undefined, isError: true } : PAGE;
  },
}));

const GROUP = { id: "g-1", name: "Paris" } as unknown as RelayGroup;

function renderTab(url = "/connections/relays/g-1/activity") {
  return renderWithProviders(
    <MemoryRouter initialEntries={[url]}>
      <ActivityTab group={GROUP} />
    </MemoryRouter>,
  );
}

const last = () => calls[calls.length - 1];

describe("Relay group activity table", () => {
  beforeEach(() => {
    calls.length = 0;
    options.length = 0;
    PAGE_STATE = { isError: false };
  });

  it("lists the requests in the shared data table with the relay name and Export CSV", () => {
    renderTab();
    const rows = screen.getAllByTestId("relay-activity-row");
    expect(rows).toHaveLength(2);
    // The request line: the method kept whole, the target in the shared
    // TruncatedText, a tab stop only when cut (it fits here), never a native title.
    const request = within(rows[0]).getByTestId("relay-activity-request");
    expect(request.textContent).toBe("GET https://ok-1.internal.example/api/v1/health");
    expect(within(request).getByText("https://ok-1.internal.example/api/v1/health").getAttribute("tabindex")).toBeNull();
    expect(rows[0].querySelector("[title]")).toBeNull();
    expect(within(rows[1]).getByText("dns_failed")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: /export csv/i }).length).toBeGreaterThan(0);
    expect(last()).toMatchObject({ limit: 50, offset: 0, direction: "desc", outcome: undefined, host: undefined, relay_id: undefined });
  });

  it("shows durations for a person to read, not raw milliseconds", () => {
    PAGE.data.items[0] = entry("ok-1", { duration_ms: 123456 });
    try {
      renderTab();
      const rows = screen.getAllByTestId("relay-activity-row");
      expect(within(rows[0]).getByText("2.1m")).toBeTruthy();
      expect(within(rows[1]).getByText("42ms")).toBeTruthy();
      expect(within(rows[0]).queryByText(/123456/)).toBeNull();
    } finally {
      PAGE.data.items[0] = entry("ok-1");
    }
  });

  it("keeps a read error in the table with Retry instead of the page boundary", () => {
    PAGE_STATE = { isError: true };
    renderTab();
    expect(options[options.length - 1]).toMatchObject({ throwOnError: false });
    expect(screen.getAllByRole("button", { name: /retry/i }).length).toBeGreaterThan(0);
  });

  it("sends the host search to the server after the table's debounce", () => {
    vi.useFakeTimers();
    try {
      renderTab();
      fireEvent.change(screen.getAllByRole("searchbox")[0], { target: { value: "vault" } });
      act(() => {
        vi.advanceTimersByTime(300);
      });
      expect(last()).toMatchObject({ host: "vault", offset: 0 });
    } finally {
      vi.useRealTimers();
    }
  });

  it("reads the outcome, relays, sort and page (1-based in the URL) from the URL", () => {
    renderTab("/connections/relays/g-1/activity?act_f_outcome=error&act_f_relay=r-1&act_f_relay=r-2&act_sort=created_at:asc&act_page=2");
    expect(last()).toMatchObject({ outcome: "error", relay_id: ["r-1", "r-2"], direction: "asc", offset: 50 });
  });

  it("filters by relay through a filter pill of the table, never a select beside it", () => {
    renderTab();
    expect(screen.queryByRole("combobox", { name: /relay/i })).toBeNull();
    fireEvent.click(screen.getAllByRole("button", { name: /^relay/i })[0]);
    fireEvent.click(screen.getByRole("button", { name: "Paris DC" }));
    expect(last()).toMatchObject({ relay_id: ["r-1"], offset: 0 });
    expect(screen.getAllByRole("button", { name: /clear all filters/i }).length).toBeGreaterThan(0);
  });

  it("asks the API with the page size the user kept", () => {
    localStorage.setItem(`${TABLE_STORAGE_PREFIX}relay-group-activity`, JSON.stringify({ pageSize: 100 }));
    try {
      renderTab();
      expect(last()).toMatchObject({ limit: 100, offset: 0 });
    } finally {
      localStorage.removeItem(`${TABLE_STORAGE_PREFIX}relay-group-activity`);
    }
  });

  it("sorts oldest first through the When header", () => {
    renderTab();
    fireEvent.click(screen.getByRole("button", { name: /when/i }));
    expect(last()).toMatchObject({ direction: "asc" });
  });
});

describe("outcome helpers", () => {
  it("narrows the log only when one outcome is selected", () => {
    expect(outcomeParam([])).toBeUndefined();
    expect(outcomeParam(["ok"])).toBe("ok");
    expect(outcomeParam(["error"])).toBe("error");
    expect(outcomeParam(["ok", "error"])).toBeUndefined();
    expect(outcomeParam(["bogus"])).toBeUndefined();
  });

  it("calls a request failed on an error code or a 5xx answer", () => {
    expect(failed(entry("a"))).toBe(false);
    expect(failed(entry("b", { status_code: 503 }))).toBe(true);
    expect(failed(entry("c", { status_code: null, error_code: "timeout" }))).toBe(true);
  });
});
