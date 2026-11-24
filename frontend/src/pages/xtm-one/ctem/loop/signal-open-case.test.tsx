// SPDX-License-Identifier: Apache-2.0
// The loop trace pivots to the case (Sam, 24 Nov 2026): a signal that opened or
// joined a case draws "Open case" on the "Case opened" stage, linked to the case
// detail page; no link without a case, and none on that case's own page.

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { CtemSignal } from "@/hooks/api/useCtemSignalsApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { SignalTraceBody, signalCaseLink } from "./SignalLoopTrace";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn() },
}));

const get = vi.mocked(api.get);
const ID = "00000000-0000-0000-0000-000000000090";

const opened: CtemSignal = {
  id: ID,
  trigger_path: "new_vulnerability",
  trigger_path_label: "New vulnerability on a running version",
  lens: "world",
  source: { provider: "opencti", external_id: "relationship--1" },
  subject: { kind: "vulnerability", ref: "vulnerability--1", label: "CVE-2026-0001" },
  observed_at: "2026-11-21T08:00:00Z",
  first_seen_at: "2026-11-21T08:00:00Z",
  last_seen_at: "2026-11-21T08:00:00Z",
  seen_count: 1,
  evidence: {},
  state: "case_opened",
  decision: { version: 1, outcome: "case_opened" },
  case_id: "case-7",
  recorded_via: "listener",
};

async function renderTrace(signal: CtemSignal, currentCaseId?: string) {
  get.mockResolvedValue(signal);
  renderWithProviders(
    <MemoryRouter>
      <SignalTraceBody id={ID} currentCaseId={currentCaseId} />
    </MemoryRouter>,
  );
  return screen.findByTestId("ctem-loop-stages");
}

afterEach(() => get.mockReset());

describe("signal case link", () => {
  it("links to the case detail page, never to the case already open, never without a case", () => {
    expect(signalCaseLink("case-7")).toBe("/ctem/cases/case-7");
    expect(signalCaseLink("case-7", "other")).toBe("/ctem/cases/case-7");
    expect(signalCaseLink("case-7", "case-7")).toBeNull();
    expect(signalCaseLink(null)).toBeNull();
  });

  it("draws Open case on the Case opened stage when the signal has a case", async () => {
    const stages = await renderTrace(opened);
    const link = within(stages).getByTestId("ctem-loop-trace-open-case");
    expect(link).toHaveAttribute("href", "/ctem/cases/case-7");
    expect(link).toHaveTextContent("Open case");
    expect(link.closest("li")?.getAttribute("data-stage")).toBe("case");
  });

  it("draws nothing when the signal has no case", async () => {
    const stages = await renderTrace({ ...opened, state: "to_validate", decision: null, case_id: null });
    expect(within(stages).queryByTestId("ctem-loop-trace-open-case")).toBeNull();
  });

  it("draws no link on that case's own page", async () => {
    const stages = await renderTrace(opened, "case-7");
    expect(within(stages).queryByTestId("ctem-loop-trace-open-case")).toBeNull();
  });
});
