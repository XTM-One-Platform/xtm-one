// The loop trace opens on the case (Sam, 24 Nov 2026): every "See the loop
// trace" / "Loop trace" button of the "Opened by" card opens the shared trace
// sheet over the case, the pathname unchanged and `?signal=<id>` added; Close,
// Escape and Back return to the case; a reload with the parameter reopens it;
// "Open in Signals" inside the sheet is the only way out of the case.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { renderWithProviders } from "@/testing/providers";
import type { CtemSignal } from "@/hooks/api/useCtemSignalsApi";

vi.mock("./loop/SignalLoopTrace", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./loop/SignalLoopTrace")>()),
  SignalTraceBody: ({ id }: { id: string }) => <p data-testid="trace-body">{id}</p>,
}));

const inScope = { reason_code: "in_scope", outcome: "case_opened", reason_params: { criticality: "high", threshold: "high", confidence: "low" } };

function signal(i: number): CtemSignal {
  const day = `2026-11-${String(1 + (i % 20)).padStart(2, "0")}T10:00:00Z`;
  return {
    id: `sig-${i}`,
    trigger_path: "vuln_on_running_version",
    trigger_path_label: "New vulnerability on a running version",
    lens: "world",
    source: { provider: "opencti", external_id: `x-${i}` },
    subject: { kind: "cve", ref: `CVE-2026-${10000 + i}`, label: `CVE-2026-${10000 + i}` },
    observed_at: day,
    first_seen_at: day,
    last_seen_at: day,
    seen_count: 1,
    evidence: {},
    state: "case_opened",
    decision: inScope,
    case_id: "c1",
    recorded_via: "listener",
  };
}

let current: CtemSignal[] = [];
vi.mock("@/hooks/api/useCtemSignalsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemSignalsApi")>()),
  useCtemCaseSignals: () => ({ data: { total: current.length, counts: {}, signals: current }, isLoading: false, isError: false }),
}));

const { CaseOpenedBy } = await import("./CaseOpenedBy");
const { LoopTraceSheet } = await import("./loop/LoopTraceSheet");

function Probe() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output data-testid="location">{`${location.pathname}${location.search}`}</output>
      <button type="button" data-testid="history-back" onClick={() => navigate(-1)}>
        history back
      </button>
    </>
  );
}

const CASE = "/ctem/cases/c1";

function renderCase(entry = CASE) {
  renderWithProviders(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/ctem/cases/:id" element={<CaseOpenedBy caseId="c1" />} />
        <Route path="/ctem/signals" element={<p data-testid="signals-page">signals</p>} />
      </Routes>
      <Probe />
    </MemoryRouter>,
  );
}

const location = () => screen.getByTestId("location").textContent ?? "";

describe("loop trace on the case", () => {
  it("opens the sheet on the case from a single signal, pathname unchanged, and Close returns to the case", async () => {
    current = [signal(0)];
    renderCase();
    fireEvent.click(screen.getByRole("button", { name: "See the loop trace of CVE-2026-10000" }));
    await waitFor(() => expect(location()).toBe(`${CASE}?signal=sig-0`));
    const sheet = screen.getByRole("dialog", { name: "Loop trace" });
    expect(within(sheet).getByTestId("trace-body")).toHaveTextContent("sig-0");
    expect(screen.queryByTestId("signals-page")).toBeNull();
    expect(screen.getByTestId("ctem-case-opened-by")).toBeInTheDocument();
    fireEvent.click(within(sheet).getByTestId("ctem-loop-trace-close"));
    await waitFor(() => expect(location()).toBe(CASE));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens the newest signal of a group and each table row on the case", async () => {
    current = Array.from({ length: 4 }, (_, i) => signal(i));
    renderCase();
    const group = screen.getByTestId("ctem-case-opened-by-group");
    const [groupButton, ...rowButtons] = within(group).getAllByTestId("ctem-case-loop-trace-button");
    // The group line opens its newest signal (the latest day: sig-3).
    fireEvent.click(groupButton);
    await waitFor(() => expect(location()).toBe(`${CASE}?signal=sig-3`));
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(location()).toBe(CASE));
    fireEvent.click(rowButtons[1]);
    await waitFor(() => expect(location()).toMatch(/^\/ctem\/cases\/c1\?signal=sig-\d$/));
    expect(screen.getByRole("dialog", { name: "Loop trace" })).toBeInTheDocument();
  });

  it("closes on Back, the case left as it was", async () => {
    current = [signal(0)];
    renderCase();
    fireEvent.click(screen.getByTestId("ctem-case-loop-trace-button"));
    await waitFor(() => expect(location()).toContain("signal=sig-0"));
    fireEvent.click(screen.getByTestId("history-back"));
    await waitFor(() => expect(location()).toBe(CASE));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByTestId("ctem-case-opened-by")).toBeInTheDocument();
  });

  it("reopens the sheet on a reload of the case URL with the parameter, and links to the trace in Signals", () => {
    current = [signal(0)];
    renderCase(`${CASE}?signal=sig-0`);
    const sheet = screen.getByRole("dialog", { name: "Loop trace" });
    expect(within(sheet).getByTestId("ctem-loop-trace-open-in-signals")).toHaveAttribute("href", "/ctem/signals?signal=sig-0");
  });
});

describe("LoopTraceSheet", () => {
  it("stays closed without a signal and draws the header, Close and the one scroller with one", () => {
    const onClose = vi.fn();
    const { rerender } = renderWithProviders(
      <MemoryRouter>
        <LoopTraceSheet signalId={null} onClose={onClose} />
      </MemoryRouter>,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    rerender(
      <MemoryRouter>
        <LoopTraceSheet signalId="sig-9" onClose={onClose} />
      </MemoryRouter>,
    );
    const sheet = screen.getByRole("dialog", { name: "Loop trace" });
    expect(within(sheet).getByRole("heading", { name: "Loop trace" })).toBeInTheDocument();
    expect(within(sheet).getByTestId("ctem-loop-trace-scroll")).toContainElement(within(sheet).getByTestId("trace-body"));
    // The Signals page draws no "Open in Signals" link: the option is off by default.
    expect(within(sheet).queryByTestId("ctem-loop-trace-open-in-signals")).toBeNull();
    fireEvent.click(within(sheet).getByTestId("ctem-loop-trace-close"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
