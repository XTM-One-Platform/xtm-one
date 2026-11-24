// "Opened by" compact (#1679 point 6, Sam 24 Nov 2026): signals of one trigger
// path, lens, source and scope decision read as one group line with a count,
// subject chips and "+N more", collapsed above five signals, expanding into a
// table where every signal keeps a labelled loop trace button.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import type { CtemSignal } from "@/hooks/api/useCtemSignalsApi";

const inScope = { reason_code: "in_scope", outcome: "case_opened", reason_params: { criticality: "high", threshold: "high", confidence: "low" } };

function signal(i: number, over: Partial<CtemSignal> = {}): CtemSignal {
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
    ...over,
  };
}

let current: CtemSignal[] = [];
vi.mock("@/hooks/api/useCtemSignalsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemSignalsApi")>()),
  useCtemCaseSignals: () => ({ data: { total: current.length, counts: {}, signals: current }, isLoading: false, isError: false }),
}));

const { CaseOpenedBy } = await import("./CaseOpenedBy");
const { groupOpenedBySignals } = await import("./opened-by-groups");

const thirty = Array.from({ length: 30 }, (_, i) => signal(i));
const otherPath = [
  signal(100, { trigger_path: "leak", trigger_path_label: "Credential leak", lens: "outside" }),
  signal(101, { trigger_path: "leak", trigger_path_label: "Credential leak", lens: "outside" }),
];

describe("groupOpenedBySignals", () => {
  it("groups 30 signals of one path, lens, source and decision into one group of 30", () => {
    const groups = groupOpenedBySignals([...thirty, ...otherPath]);
    expect(groups).toHaveLength(2);
    expect(groups[0].signals).toHaveLength(30);
    expect(groups[1].signals).toHaveLength(2);
  });

  it("splits a group when the scope decision differs", () => {
    const below = signal(50, { decision: { reason_code: "below_scope_threshold", reason_params: { criticality: "low", threshold: "high" } } });
    expect(groupOpenedBySignals([...thirty, below])).toHaveLength(2);
  });
});

describe("CaseOpenedBy compact", () => {
  it("draws one collapsed group line with a count, the decision once and +N more", () => {
    current = [...thirty, ...otherPath];
    renderWithProviders(<CaseOpenedBy caseId="c1" />);
    const group = screen.getByTestId("ctem-case-opened-by-group");
    expect(within(group).getByTestId("ctem-case-opened-by-group-count")).toHaveTextContent("30 signals");
    expect(within(group).getAllByText(/In scope: criticality High \(threshold High\), confidence Low/)).toHaveLength(1);
    expect(within(group).getByTestId("ctem-case-opened-by-subjects-more")).toHaveTextContent("+26 more");
    expect(screen.queryByTestId("ctem-case-opened-by-table")).toBeNull();
    // The two signals of the other path keep the per-signal detail.
    expect(screen.getAllByTestId("ctem-case-opened-by-signal")).toHaveLength(2);
  });

  it("expands to every signal, each with a labelled loop trace button naming its subject", () => {
    current = [...thirty];
    renderWithProviders(<CaseOpenedBy caseId="c1" />);
    const toggle = screen.getByTestId("ctem-case-opened-by-toggle");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const table = screen.getByTestId("ctem-case-opened-by-table");
    expect(within(table).getAllByTestId("ctem-case-opened-by-signal")).toHaveLength(30);
    const buttons = within(table).getAllByTestId("ctem-case-loop-trace-button");
    expect(buttons).toHaveLength(30);
    expect(buttons[0]).toHaveTextContent("Loop trace");
    expect(buttons[0].getAttribute("href")).toMatch(/\/ctem\/signals\?signal=sig-/);
    expect(screen.getByRole("link", { name: "See the loop trace of CVE-2026-10000" })).toBeInTheDocument();
  });

  it("filters the expanded table above ten signals", () => {
    current = [...thirty];
    renderWithProviders(<CaseOpenedBy caseId="c1" />);
    fireEvent.click(screen.getByTestId("ctem-case-opened-by-toggle"));
    fireEvent.change(screen.getByTestId("ctem-case-opened-by-filter"), { target: { value: "10007" } });
    expect(within(screen.getByTestId("ctem-case-opened-by-table")).getAllByTestId("ctem-case-opened-by-signal")).toHaveLength(1);
  });

  it("opens a group of four or five by default and keeps the detail of one to three", () => {
    current = thirty.slice(0, 4);
    renderWithProviders(<CaseOpenedBy caseId="c1" />);
    expect(screen.getByTestId("ctem-case-opened-by-table")).toBeInTheDocument();
  });

  it("draws the loop trace of a single signal as a button, never a bare link", () => {
    current = [thirty[0]];
    renderWithProviders(<CaseOpenedBy caseId="c1" />);
    const button = screen.getByTestId("ctem-case-loop-trace-button");
    expect(button).toHaveTextContent("See the loop trace");
    expect(button).toHaveAccessibleName("See the loop trace of CVE-2026-10000");
  });
});
