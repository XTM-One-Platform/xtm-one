// The Signals page (#1263): routes, filters in the URL,
// the loop trace as a section of the page and the empty states.

import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import type { CtemSignal, CtemSignalPath } from "@/hooks/api/useCtemSignalsApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import CtemSignals from "../../CtemSignals";
import { readSignalsView, selectAllScope, signalsHref } from "./signals-route";

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

// The bulk bar shows two actions inline from 1440 px, one below (UX round 2 on #1521, B1).
const originalMatchMedia = window.matchMedia;
function setCompactToolbar(compact: boolean) {
  window.matchMedia = ((query: string) => ({
    matches: compact && query === "(max-width: 1439px)",
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}
afterEach(() => {
  window.matchMedia = originalMatchMedia;
});

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

  // #1505: unmatched signals are dismissed in one call, the selected rows or every one of the state.
  it("dismisses every unmatched signal of the state in one call", async () => {
    const signals = Array.from({ length: 3 }, (_, i) => signal(i + 1));
    const post = vi.mocked(api.post);
    post.mockResolvedValue({ matched: 120, counts: { changed: 120, skipped: 0, discarded: 120 } });
    // The page holds 3 rows; the state counts 120: "select all" reaches past the page.
    renderPage("/ctem/signals?state=to_validate", { total: 3, counts: { to_validate: 120 }, signals });
    await screen.findAllByTestId("ctem-signal-row");
    fireEvent.click(screen.getAllByRole("checkbox", { name: "Select every row on this page" })[0]);
    const bar = await screen.findByTestId("ctem-signals-bulk-actions");
    fireEvent.click(within(bar).getByRole("button", { name: /select all 120/ }));
    fireEvent.click(within(screen.getByTestId("ctem-signals-bulk-actions")).getByTestId("ctem-signals-dismiss"));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/ctem-signals/triage", { action: "dismiss", filter: { state: "to_validate" } }),
    );
    post.mockReset();
  });

  // Review on #1521: "select all" covers what the table shows, never rows its filters hide.
  it("sends the trigger path filter with select all, and offers no select all under a search or a source filter", async () => {
    expect(selectAllScope(new URLSearchParams("state=to_validate"))).toEqual({});
    expect(selectAllScope(new URLSearchParams("s_f_path=new_exposure"))).toEqual({ trigger_path: "new_exposure" });
    expect(selectAllScope(new URLSearchParams("s_f_path=new_exposure&s_f_path=code_flaw"))).toBeNull();
    expect(selectAllScope(new URLSearchParams("s_f_source=shodan"))).toBeNull();
    expect(selectAllScope(new URLSearchParams("s_q=host-1"))).toBeNull();

    // 30 signals of the path, 2 of another: the page shows 25, "select all 30" names the path.
    const signals = [
      ...Array.from({ length: 30 }, (_, i) => signal(i + 1)),
      ...Array.from({ length: 2 }, (_, i) => signal(i + 31, { trigger_path: "code_flaw", trigger_path_label: "Code flaw" })),
    ];
    const post = vi.mocked(api.post);
    post.mockResolvedValue({ matched: 30, counts: { changed: 30, skipped: 0, discarded: 30 } });
    renderPage("/ctem/signals?state=to_validate&s_f_path=new_exposure", { total: 32, counts: { to_validate: 32 }, signals });
    await screen.findAllByTestId("ctem-signal-row");
    fireEvent.click(screen.getAllByRole("checkbox", { name: "Select every row on this page" })[0]);
    const bar = await screen.findByTestId("ctem-signals-bulk-actions");
    fireEvent.click(within(bar).getByRole("button", { name: /select all 30/ }));
    fireEvent.click(within(screen.getByTestId("ctem-signals-bulk-actions")).getByTestId("ctem-signals-dismiss"));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/ctem-signals/triage", {
        action: "dismiss",
        filter: { state: "to_validate", trigger_path: "new_exposure" },
      }),
    );
    post.mockReset();
  });

  it("offers no select all while the table is searched", async () => {
    const signals = Array.from({ length: 3 }, (_, i) => signal(i + 1));
    renderPage("/ctem/signals?state=to_validate&s_q=host", { total: 3, counts: { to_validate: 120 }, signals });
    await screen.findAllByTestId("ctem-signal-row");
    fireEvent.click(screen.getAllByRole("checkbox", { name: "Select every row on this page" })[0]);
    const bar = await screen.findByTestId("ctem-signals-bulk-actions");
    expect(within(bar).queryByRole("button", { name: /select all/ })).toBeNull();
  });

  it("checks the selected rows again by id", async () => {
    setCompactToolbar(false);
    const signals = Array.from({ length: 2 }, (_, i) => signal(i + 1));
    const post = vi.mocked(api.post);
    post.mockResolvedValue({ matched: 1, counts: { changed: 0, skipped: 0, to_validate: 1 } });
    renderPage("/ctem/signals?state=to_validate", { total: 2, counts: { to_validate: 2 }, signals });
    await screen.findAllByTestId("ctem-signal-row");
    fireEvent.click(screen.getAllByRole("checkbox", { name: "Select this row" })[0]);
    fireEvent.click(await screen.findByTestId("ctem-signals-recheck"));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/ctem-signals/triage", { action: "recheck", ids: [expect.any(String)] }));
    expect(screen.queryByTestId("ctem-signals-restore")).toBeNull();
    post.mockReset();
  });

  // UX round 1 on #1521, B2: a dismissed signal stays dismissed on a re-check, so a selection of
  // dismissed signals alone is offered Restore, never Check again or Dismiss.
  it("offers only Restore for a selection of dismissed signals", async () => {
    const dismissed = { state: "discarded" as const, decision: { reason_code: "dismissed_by_user" } as CtemSignal["decision"] };
    const signals = Array.from({ length: 2 }, (_, i) => signal(i + 1, dismissed));
    renderPage("/ctem/signals?state=discarded", { total: 2, counts: { discarded: 2 }, signals });
    await screen.findAllByTestId("ctem-signal-row");
    fireEvent.click(screen.getAllByRole("checkbox", { name: "Select this row" })[0]);
    const bar = await screen.findByTestId("ctem-signals-bulk-actions");
    expect(within(bar).getByTestId("ctem-signals-restore")).toBeTruthy();
    expect(within(bar).queryByTestId("ctem-signals-recheck")).toBeNull();
    expect(within(bar).queryByTestId("ctem-signals-dismiss")).toBeNull();
    expect(within(bar).queryByRole("button", { name: "More actions" })).toBeNull();
  });

  it("says nothing changed instead of a success when a triage changes no signal", async () => {
    setCompactToolbar(false);
    const signals = [signal(1)];
    const post = vi.mocked(api.post);
    post.mockResolvedValue({ matched: 1, counts: { changed: 0, skipped: 1 } });
    renderPage("/ctem/signals?state=to_validate", { total: 1, counts: { to_validate: 1 }, signals });
    await screen.findAllByTestId("ctem-signal-row");
    fireEvent.click(screen.getAllByRole("checkbox", { name: "Select this row" })[0]);
    fireEvent.click(await screen.findByTestId("ctem-signals-recheck"));
    expect(await screen.findByText("No signal changed")).toBeTruthy();
    expect(screen.queryByText(/checked again/)).toBeNull();
    post.mockReset();
  });

  // UX round 2 on #1521, B1: at 1080 a selection keeps the toolbar on one 32 px row, so one
  // action shows inline and the others are rows of More actions.
  it("shows one bulk action inline below 1440 px, the rest under More actions", async () => {
    setCompactToolbar(true);
    const signals = Array.from({ length: 2 }, (_, i) => signal(i + 1));
    renderPage("/ctem/signals?state=to_validate", { total: 2, counts: { to_validate: 2 }, signals });
    await screen.findAllByTestId("ctem-signal-row");
    // Every row of the state selected: no "select all" link, so one action shows inline.
    fireEvent.click(screen.getAllByRole("checkbox", { name: "Select every row on this page" })[0]);
    const bar = await screen.findByTestId("ctem-signals-bulk-actions");
    expect(within(bar).getByTestId("ctem-signals-dismiss")).toBeTruthy();
    expect(within(bar).queryByTestId("ctem-signals-recheck")).toBeNull();
    expect(within(bar).getByRole("button", { name: "More actions" })).toBeTruthy();
  });

  // UX round 4 on #1521, B1: at 1080 with a trigger path filter and a selection, the "select all N"
  // link takes the inline slot and every action is a row of More actions, so the bar keeps one row.
  it("moves every bulk action under More actions below 1440 px while select all is offered", async () => {
    setCompactToolbar(true);
    const signals = Array.from({ length: 8 }, (_, i) => signal(i + 1));
    renderPage("/ctem/signals?state=to_validate&s_f_path=new_exposure", { total: 8, counts: { to_validate: 9 }, signals });
    await screen.findAllByTestId("ctem-signal-row");
    fireEvent.click(screen.getAllByRole("checkbox", { name: "Select this row" })[0]);
    const bar = await screen.findByTestId("ctem-signals-bulk-actions");
    expect(within(bar).getByRole("button", { name: /select all 8/ })).toBeTruthy();
    expect(within(bar).queryByTestId("ctem-signals-dismiss")).toBeNull();
    expect(within(bar).queryByTestId("ctem-signals-recheck")).toBeNull();
    expect(within(bar).getByRole("button", { name: "More actions" })).toBeTruthy();
    // Once every row of the path is selected, the link is gone and one action is back inline.
    fireEvent.click(within(bar).getByRole("button", { name: /select all 8/ }));
    expect(within(screen.getByTestId("ctem-signals-bulk-actions")).getByTestId("ctem-signals-dismiss")).toBeTruthy();
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

  it("sits under the header, above the list, with the key row's counts and the connected sources' logos", async () => {
    renderPage("/ctem/signals", { total: 10, counts: { to_validate: 3, new: 2, case_opened: 1, watchlist: 4 }, signals: [] }, paths);
    const strip = await screen.findByTestId("ctem-signals-summary");
    const sourcesButton = screen.getByTestId("ctem-signals-sources");
    expect(sourcesButton.compareDocumentPosition(strip) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
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

  it("opens the signals on the state a card names", async () => {
    renderPage("/ctem/signals", { total: 3, counts: { to_validate: 3 }, signals: [] }, paths);
    const strip = await screen.findByTestId("ctem-signals-summary");
    const card = await within(strip).findByRole("button", { name: /Unmatched/ });
    fireEvent.click(card);
    await waitFor(() => expect(location()).toBe("/ctem/signals?state=to_validate"));
  });

  it("links the Sources page from the connected sources card", async () => {
    renderPage("/ctem/signals", { total: 0, counts: {}, signals: [] }, paths);
    fireEvent.click(await screen.findByTestId("ctem-signals-summary-sources-link"));
    await waitFor(() => expect(location()).toBe("/ctem/signals/sources"));
  });
});

describe("Signal sources page (#1519)", () => {
  it("draws no tabs on the list and opens the sources from the header button, with the connected count", async () => {
    renderPage("/ctem/signals", { total: 0, counts: {}, signals: [] });
    expect(screen.queryByRole("tablist")).toBeNull();
    const button = await screen.findByTestId("ctem-signals-sources");
    expect(button.textContent).toContain("Sources");
    await waitFor(() => expect(screen.getByTestId("ctem-signals-sources-count").textContent).toBe("1/2"));
    fireEvent.click(button);
    await waitFor(() => expect(location()).toBe("/ctem/signals/sources"));
    expect(await screen.findByTestId("ctem-signal-sources-page")).toBeTruthy();
  });

  it("draws its own page: the back link first, the title, the sources, no summary strip and no tabs", async () => {
    renderPage("/ctem/signals/sources", { total: 0, counts: {}, signals: [] });
    const page = await screen.findByTestId("ctem-signal-sources-page");
    const back = within(page).getByRole("button", { name: "Back to Signals" });
    expect(page.firstElementChild).toBe(back);
    expect(within(page).getByRole("heading", { name: "Signal sources" })).toBeTruthy();
    expect(screen.queryByTestId("ctem-signals-summary")).toBeNull();
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.queryByTestId("ctem-signals-page")).toBeNull();
    fireEvent.click(back);
    await waitFor(() => expect(location()).toBe("/ctem/signals"));
    expect(await screen.findByTestId("ctem-signals-page")).toBeTruthy();
  });
});
