// The Signals page (#1263): routes, filters in the URL, the old dialog links,
// the loop trace as a section of the page and the empty states.

import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import type { CtemSignal, CtemSignalPath } from "@/hooks/api/useCtemSignalsApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import CtemSignals from "../../CtemSignals";
import { LegacySignalLinks } from "./LegacySignalLinks";
import { legacySignalsHref, readSignalsView, signalsHref } from "./signals-route";

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
        <Route path="*" element={<LegacySignalLinks />} />
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

  it("maps every old dialog link to the page with the same filter", () => {
    expect(legacySignalsHref(new URLSearchParams("loop=signals:watchlist"))).toBe("/ctem/signals?state=watchlist");
    expect(legacySignalsHref(new URLSearchParams("loop=signals:all:code_flaw"))).toBe("/ctem/signals?s_f_path=code_flaw");
    expect(legacySignalsHref(new URLSearchParams(`loop=signal:${ID}`))).toBe(`/ctem/signals?signal=${ID}`);
    expect(legacySignalsHref(new URLSearchParams(`loop=case:${ID}`))).toBe(`/ctem/cases/${ID}`);
    expect(legacySignalsHref(new URLSearchParams("signal-sources=1"))).toBe("/ctem/signals/sources");
    expect(legacySignalsHref(new URLSearchParams("tab=x"))).toBeNull();
  });

  it("redirects an old command center link to the filtered page", async () => {
    renderPage("/?loop=signals:to_validate", { total: 1, counts: { to_validate: 1 }, signals: [signal(1)] });
    await waitFor(() => expect(location()).toBe("/ctem/signals?state=to_validate"));
    expect(await screen.findByTestId("ctem-signals-page")).toBeTruthy();
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
    expect(await screen.findByTestId("ctem-loop-trace")).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
    // The trace body sits in its own scroll box, so the table keeps its room (UX r1 B1 on #1272).
    expect(screen.getByTestId("ctem-loop-trace").contains(screen.getByTestId("ctem-loop-trace-scroll"))).toBe(true);
    fireEvent.click(screen.getByTestId("ctem-loop-trace-close"));
    await waitFor(() => expect(location()).not.toContain("signal="));
  });

  it("says no source is connected and links to the Sources tab when nothing exists", async () => {
    renderPage("/ctem/signals", { total: 0, counts: {}, signals: [] }, [PATHS[1]]);
    const empty = await screen.findByTestId("ctem-signal-empty");
    expect(within(empty).getByText("No signal yet: no source is connected.")).toBeTruthy();
    fireEvent.click(within(empty).getByTestId("ctem-signal-sources-link"));
    await waitFor(() => expect(location()).toBe("/ctem/signals/sources"));
  });

  it("says the sources are connected and nothing was seen when one is", async () => {
    renderPage("/ctem/signals", { total: 0, counts: {}, signals: [] });
    const empty = await screen.findByTestId("ctem-signal-empty");
    expect(within(empty).getByText("No signal seen yet: your sources are connected and nothing reached the loop.")).toBeTruthy();
  });
});
