// SPDX-License-Identifier: Apache-2.0
// CTEM-28-UI: blind spots and evidence decay read in the loop trace: the state
// in words, "No evidence since" with a date or "Never measured" (a gap never
// reads as safe), the risk scenario and the sources, and an emptied connector
// sync says its findings stay open; never as raw evidence lines.

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { CtemSignal } from "@/hooks/api/useCtemSignalsApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { SignalTraceBody } from "./SignalLoopTrace";
import { evidenceHealthFacts } from "./evidence-health";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn() },
}));

const get = vi.mocked(api.get);
const ID = "00000000-0000-0000-0000-000000000280";

const expired: CtemSignal = {
  id: ID,
  trigger_path: "control_decay",
  trigger_path_label: "Control or evidence decay",
  lens: "inside",
  source: { provider: "opencrq", external_id: "" },
  subject: { kind: "asset", ref: "billing-db", label: "billing-db: no evidence within the re-test delay" },
  observed_at: "2026-11-22T05:00:00Z",
  first_seen_at: "2026-11-22T05:00:00Z",
  last_seen_at: "2026-11-22T05:00:00Z",
  seen_count: 1,
  evidence: {
    event: "evidence_expired",
    state: "expired",
    last_evidence_at: "2026-09-14T08:00:00Z",
    opencrq_asset_ids: ["a-1"],
    risk_scenario_id: "s-1",
    risk_scenario: "Ransomware on billing",
    impact: "high",
    sources: [{ origin: "tenable", last_seen_at: "2026-09-14T08:00:00Z" }],
  },
  state: "confirmed",
  decision: { version: 1, outcome: "in_scope" },
  case_id: null,
  recorded_via: "system",
};

function renderTrace(signal: CtemSignal) {
  get.mockResolvedValue(signal);
  renderWithProviders(
    <MemoryRouter initialEntries={[`/ctem/signals?signal=${ID}`]}>
      <SignalTraceBody id={ID} />
    </MemoryRouter>,
  );
  return screen.findByTestId("ctem-signal-facts");
}

afterEach(() => get.mockReset());

describe("evidence health signal facts", () => {
  it("reads only the signals of the evidence health pass", () => {
    expect(evidenceHealthFacts(expired.evidence)?.state).toBe("expired");
    expect(evidenceHealthFacts({ event: "coverage_gap", state: "no_source" })).toEqual({
      state: "no_source", lastEvidence: null, scenario: null, sources: [], sync: null,
    });
    expect(evidenceHealthFacts({ event: "issue.open.created", state: "expired" })).toBeNull();
    expect(evidenceHealthFacts({ event: "evidence_expired", state: "current" })).toBeNull();
    expect(evidenceHealthFacts(null)).toBeNull();
  });

  it("an expired asset names its state, the date of its last evidence and its sources", async () => {
    const facts = await renderTrace(expired);
    expect(within(facts).getByText("Evidence expired: older than the re-test delay")).toBeTruthy();
    expect(within(facts).getByText("No evidence since")).toBeTruthy();
    expect(within(facts).getByText("Ransomware on billing")).toBeTruthy();
    expect(within(facts).getByText("Evidence sources")).toBeTruthy();
    expect(within(facts).getByText(/^Tenable, last reported /)).toBeTruthy();
    expect(within(facts).queryByText("Sources")).toBeNull();
    const evidence = within(facts).queryByTestId("ctem-signal-evidence");
    expect(evidence?.textContent ?? "").not.toContain("last_evidence_at");
    expect(evidence?.textContent ?? "").not.toContain("risk_scenario");
  });

  it("a blind spot with no evidence reads never measured and no source, never a dash", async () => {
    const facts = await renderTrace({
      ...expired,
      trigger_path: "blind_spot",
      trigger_path_label: "Blind spot or coverage gap",
      evidence: { event: "coverage_gap", state: "no_source", last_evidence_at: null, risk_scenario: "Ransomware on billing", sources: [] },
    });
    expect(within(facts).getByText("Blind spot: no source reports this asset")).toBeTruthy();
    expect(within(facts).getByText("Never measured")).toBeTruthy();
    // The state already says it: no second row repeating it.
    expect(within(facts).queryByText("No source reports this asset")).toBeNull();
    expect(within(facts).queryByText("Evidence sources")).toBeNull();
  });

  it("never shows the backend reason sentence as a raw evidence line", async () => {
    const facts = await renderTrace({
      ...expired,
      evidence: { ...expired.evidence, reason: "no evidence within the re-test delay" },
    });
    expect(within(facts).getByText("Evidence expired: older than the re-test delay")).toBeTruthy();
    const evidence = within(facts).queryByTestId("ctem-signal-evidence");
    expect(evidence?.textContent ?? "").not.toContain("reason");
    expect(evidence?.textContent ?? "").not.toContain("no evidence within the re-test delay");
  });

  it("an emptied connector sync gives its row counts and keeps its findings open", async () => {
    const facts = await renderTrace({
      ...expired,
      trigger_path: "blind_spot",
      trigger_path_label: "Blind spot or coverage gap",
      subject: { kind: "asset", ref: "opencrq:tenable", label: "the tenable sync emptied" },
      evidence: { event: "coverage_gap", state: "source_emptied", sync_kind: "tenable", last_run_at: "2026-11-21T02:00:00Z", latest_total: 0, previous_total: 412 },
    });
    expect(within(facts).getByText("Blind spot: a connector sync emptied")).toBeTruthy();
    expect(within(facts).getByText("Tenable: 0 rows after 412; its findings stay open")).toBeTruthy();
    expect(within(facts).queryByText("No evidence since")).toBeNull();
  });
});
