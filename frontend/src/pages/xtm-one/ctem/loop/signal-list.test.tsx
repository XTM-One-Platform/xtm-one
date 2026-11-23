// The Signals page (#1263): routes, filters in the URL,
// the loop trace as a section of the page and the empty states.

import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import type { CtemSignal, CtemSignalPath } from "@/hooks/api/useCtemSignalsApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import CtemSignals from "../../CtemSignals";
import { readSignalsView, signalsHref } from "./signals-route";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));

// The tabs register palette commands; the palette is not under test here.
vi.mock("@/lib/commands/CommandsProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/commands/CommandsProvider")>()),
  useCommandSections: () => {},
}));

const get = vi.mocked(api.get);
const ID = "00000000-0000-0000-0000-000000000001";

function signal(n: number, over: Partial<CtemSignal> = {}): CtemSignal {
  return {
    id: `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`,
    trigger_path: "new_exposure",
    trigger_path_label: "New exposure",
    lens: "outside",
    source: { provider: "shodan", external_id: `host-${n}` },
    subject: { kind: "asset", ref: `host-${n}.example.test`, label: `host-${n}.example.test` },
    observed_at: "2026-11-21T08:00:00Z",
    first_seen_at: "2026-11-21T08:00:00Z",
    last_seen_at: "2026-11-21T08:05:00Z",
    seen_count: 1,
    evidence: {},
    state: "to_validate",
    decision: null,
    case_id: null,
    recorded_via: "api",
    ...over,
  };
}

function path(over: Partial<CtemSignalPath>): CtemSignalPath {
  return { id: "x", label: "X", default_lens: "inside", listener: false, counts: {}, total: 0, configured: false, ...over };
}

const PATHS: CtemSignalPath[] = [
  path({ id: "new_exposure", label: "New exposure", default_lens: "outside", status: "connected", sources: [{ provider: "shodan", kind: "pass", event: "A host appears", bound: true }] }),
  path({ id: "code_flaw", label: "Code flaw", status: "not_connected", sources: [{ provider: "aikido", kind: "listener", event: "A leaked secret issue", bound: false }] }),
];

function Probe() {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>;
}

function renderPage(entry: string, list: { total: number; counts: Record<string, number>; signals: CtemSignal[] }, paths = PATHS) {
  get.mockImplementation(async (url: string) => {
    if (url.startsWith("/ctem-signals/paths")) return { paths };
    if (url.startsWith(`/ctem-signals/${ID}`)) return signal(1);
    return list;
  });
  renderWithProviders(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/ctem/signals" element={<CtemSignals />} />
        <Route path="/ctem/signals/:tab" element={<CtemSignals />} />
      </Routes>
      <Probe />
    </MemoryRouter>,
  );
}

const location = () => screen.getByTestId("location").textContent ?? "";

afterEach(() => get.mockReset());

describe("signals routes", () => {
  it("puts the state, the path and the open signal in the URL", () => {
    expect(signalsHref()).toBe("/ctem/signals");
    expect(signalsHref({ state: "watchlist", path: "code_flaw" })).toBe("/ctem/signals?state=watchlist&s_f_path=code_flaw");
    expect(readSignalsView(new URLSearchParams("state=bogus&s_f_path=code_flaw&signal=abc"))).toEqual({ state: null, path: "code_flaw", signal: "abc" });
  });
});

describe("Signals page", () => {
  it("lists every signal of the state, the API's counts on the filter, and opens a loop trace on the page", async () => {
    const signals = Array.from({ length: 3 }, (_, i) => signal(i + 1));
    renderPage("/ctem/signals?state=to_validate", { total: 3, counts: { to_validate: 3, new: 2 }, signals });
    const rows = await screen.findAllByTestId("ctem-signal-row");
    expect(rows).toHaveLength(3);
    expect(get).toHaveBeenCalledWith(expect.stringContaining("state=to_validate"));
    fireEvent.click(rows[0]);
    await waitFor(() => expect(location()).toContain("signal="));
    const trace = await screen.findByTestId("ctem-loop-trace");
    // The trace opens in the side sheet, out of the page flow: nothing is inserted above the table,
    // so the table stays the page's last block and keeps its own scroller (UX r2 B1 on #1272).
    const sheet = screen.getByRole("dialog", { name: "Loop trace" });
    expect(sheet.contains(trace)).toBe(true);
    expect(sheet.contains(rows[0])).toBe(false);
    expect(trace.contains(screen.getByTestId("ctem-loop-trace-scroll"))).toBe(true);
    fireEvent.click(screen.getByTestId("ctem-loop-trace-close"));
    await waitFor(() => expect(location()).not.toContain("signal="));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("closes the loop trace on Escape", async () => {
    const signals = [signal(1)];
    renderPage(`/ctem/signals?signal=${signals[0].id}`, { total: 1, counts: { to_validate: 1 }, signals });
    expect(await screen.findByRole("dialog", { name: "Loop trace" })).toBeTruthy();
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(location()).not.toContain("signal="));
  });

  it("says no source is connected when nothing exists, the table's empty statement alone", async () => {
    renderPage("/ctem/signals", { total: 0, counts: {}, signals: [] }, [PATHS[1]]);
    expect(await screen.findByText("No signal yet: no source is connected.")).toBeTruthy();
    // No paths line and no link under it (Sam, 23 Nov 2026, #1344): the strip and the Sources tab carry them.
    expect(screen.queryByTestId("ctem-signal-empty-paths")).toBeNull();
    expect(screen.queryByTestId("ctem-signal-sources-link")).toBeNull();
  });

  it("says the sources are connected and nothing was seen when one is", async () => {
    renderPage("/ctem/signals", { total: 0, counts: {}, signals: [] });
    await waitFor(() =>
      expect(screen.getByText("No signal seen yet: your sources are connected and nothing reached the loop.")).toBeTruthy(),
    );
  });
});

describe("Signals summary strip", () => {
  const paths: CtemSignalPath[] = [
    { ...PATHS[0], count_30d: 4, total: 9 },
    { ...PATHS[1], count_30d: 1, total: 1 },
  ];

  it("sits above the tabs on both tabs, with the key row's counts and the connected sources' logos", async () => {
    renderPage("/ctem/signals/sources", { total: 10, counts: { to_validate: 3, new: 2, case_opened: 1, watchlist: 4 }, signals: [] }, paths);
    const strip = await screen.findByTestId("ctem-signals-summary");
    const tabs = screen.getByTestId("ctem-signals-tabs");
    expect(strip.compareDocumentPosition(tabs) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await waitFor(() => expect(within(strip).getByText("5")).toBeTruthy());
    expect(within(strip).getByText("10 signals in all")).toBeTruthy();
    expect(within(strip).getByText("3")).toBeTruthy();
    expect(within(strip).getByText("2 new")).toBeTruthy();
    expect(within(strip).getByText("4 on the watchlist")).toBeTruthy();
    expect(within(strip).getByText("1/2")).toBeTruthy();
    const logos = within(screen.getByRole("list", { name: "1 source connected" })).getAllByRole("listitem");
    expect(logos.map((l) => l.getAttribute("data-provider"))).toEqual(["shodan"]);
  });

  it("keeps the four cards footerless, the logos and the Sources link on the fourth card's subtitle line (UX r4 B2 on #1272)", async () => {
    renderPage("/ctem/signals", { total: 10, counts: { to_validate: 3 }, signals: [] }, paths);
    const strip = await screen.findByTestId("ctem-signals-summary");
    expect(within(strip).getAllByTestId("stat-card-value")).toHaveLength(4);
    expect(within(strip).queryAllByTestId("stat-card-footer")).toHaveLength(0);
    const sources = within(strip).getByTestId("ctem-signals-summary-sources");
    expect(sources.closest("p")).not.toBeNull();
    expect(within(sources).getByTestId("ctem-signals-summary-sources-link").textContent).toBe("Sources");
  });

  it("opens the Signals tab on the state a card names", async () => {
    renderPage("/ctem/signals/sources", { total: 3, counts: { to_validate: 3 }, signals: [] }, paths);
    const strip = await screen.findByTestId("ctem-signals-summary");
    const card = await within(strip).findByRole("button", { name: /To validate/ });
    fireEvent.click(card);
    await waitFor(() => expect(location()).toBe("/ctem/signals?state=to_validate"));
  });

  it("links the Sources tab from the connected sources card", async () => {
    renderPage("/ctem/signals", { total: 0, counts: {}, signals: [] }, paths);
    fireEvent.click(await screen.findByTestId("ctem-signals-summary-sources-link"));
    await waitFor(() => expect(location()).toBe("/ctem/signals/sources"));
  });
});
